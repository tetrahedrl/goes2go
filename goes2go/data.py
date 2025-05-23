## Brian Blaylock
## September 16, 2020

"""
=============
Retrieve Data.
=============

Download and read data from the R-series Geostationary Operational
Environmental Satellite data.

Data is downloaded from Amazon Web Services and can be returned
as a file list or read as an xarray.Dataset. If the data is not
available in a local directory, it is loaded directly into memory.

https://registry.opendata.aws/noaa-goes/
"""

import multiprocessing
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path

import numpy as np
import pandas as pd
import s3fs
import xarray as xr
import xarrayfits

from goes2go.tools import lat_lon_to_scan_angles

# NOTE: These config dict values are retrieved from __init__ and read
# from the file ${HOME}/.config/goes2go/config.toml
from . import config

# Connect to AWS public buckets
fs = s3fs.S3FileSystem(anon=True)

# Define parameter options and aliases
# ------------------------------------
_satellite = {
    "noaa-goes16": [16, "16", "G16", "EAST", "GOES16"],
    "noaa-goes17": [17, "17", "G17", "WEST", "GOES17"],
    "noaa-goes18": [18, "18", "G18", "WEST", "GOES18"],
    "noaa-goes19": [19, "19", "G19", "EAST", "GOES19"],
}

_bucket = {
    "noaa-goes16": ["noaa-goes16"],
    "noaa-goes17": ["noaa-goes17"],
    "noaa-goes18": ["noaa-goes18"],
    "noaa-goes19": ["noaa-goes19","noaa-nesdis-swfo-ccor-1-pds/SWFO/GOES-19/CCOR-1"],
}

_goes_pathspec = "{bucket}/{product}/{DATE:%Y/%j/%H/}"
_swfo_pathspec = "{bucket}/{product}/{DATE:%Y/%m/%d/}"
_pathspec = {
    "noaa-goes16": _goes_pathspec,
    "noaa-goes17": _goes_pathspec,
    "noaa-goes18": _goes_pathspec,
    "noaa-goes19": _goes_pathspec,
    "noaa-nesdis-swfo-ccor-1-pds/SWFO/GOES-19/CCOR-1": _swfo_pathspec
}

_goes_datespec = "%Y%j%H%M%S%f"
_swfo_datespec = "%Y%m%dT%H%M%S"
_datespec = {
    "noaa-goes16": _goes_datespec,
    "noaa-goes17": _goes_datespec,
    "noaa-goes18": _goes_datespec,
    "noaa-goes19": _goes_datespec,
    "noaa-nesdis-swfo-ccor-1-pds/SWFO/GOES-19/CCOR-1": _swfo_datespec
}

_goes_namespec = r".*/(?P<product>[^/]*)_s(?P<start>\d{14})_e(?P<end>\d{14})_c(?P<creation>\d{14})\.(?P<filetype>.*)"
_swfo_namespec = r".*/(?P<product>[^/]*)_(?P<start>\d{8}T\d{6})_(?P<version>V\d\d)_(?P<sucode>.{2})\.(?P<filetype>.*)"
_namespec = {
    "noaa-goes16": _goes_namespec,
    "noaa-goes17": _goes_namespec,
    "noaa-goes18": _goes_namespec,
    "noaa-goes19": _goes_namespec,
    "noaa-nesdis-swfo-ccor-1-pds/SWFO/GOES-19/CCOR-1": _swfo_namespec
}

_reader = {
    "fits":lambda x: list(xarrayfits.xds_from_fits(x))[0],
    "nc":xr.load_dataset
}

_domain = {
    "C": ["CONUS"],
    "F": ["FULL", "FULLDISK", "FULL DISK"],
    "M": ["MESOSCALE", "M1", "M2"],
}

_bucket_product = {
    s:
    {
        b:
        [
            i.split("/")[-1]
            for i in fs.ls(b)
        ]
        for b in _bucket[s]
    }
    for s in _satellite
}

_product = {
    s:
    {
        i.split("/")[-1]: []
        for i in sum([fs.ls(b) for b in _bucket[s]],[])
    }
    for s in _satellite
}

for prodd in _product.values():
    prodd.pop("index.html", None)
    prodd["GLM-L2-LCFA"] = ["GLM"]
    prodd["ABI-L2-MCMIPC"] = ["ABIC"]
    prodd["ABI-L2-MCMIPF"] = ["ABIF"]
    prodd["ABI-L2-MCMIPM"] = ["ABIM"]

def _check_param_inputs(**params):
    """Check the input parameters for correct name or alias.

    Specifically, check the input for product, domain, and satellite are
    in the list of accepted values. If not, then look if it has an alias.
    """
    # Kinda messy, but gets the job done.
    params.setdefault("verbose", True)
    satellite = params["satellite"]
    domain = params["domain"]
    product = params["product"]
    # verbose = params["verbose"]

    ## Determine the Satellite
    if satellite not in _satellite:
        satellite = str(satellite).upper()
        for key, aliases in _satellite.items():
            if satellite in aliases:
                satellite = key
    if satellite not in _satellite:
        raise ValueError(f"satellite must be one of {list(_satellite.keys())} or an alias {list(_satellite.values())}")

    ## Determine the Domain (only needed for ABI product)
    if product.upper().startswith("ABI"):
        if product[-1] in _domain:
            # If the product has the domain, this takes priority
            domain = product[-1]
        elif isinstance(domain, str):
            domain = domain.upper()
            if domain in ["M1", "M2"]:
                product = product + "M"
            else:
                for key, aliases in _domain.items():
                    if domain in aliases:
                        domain = key
                product = product + domain
        if (domain not in _domain) and (domain not in ["M1", "M2"]):
            raise ValueError(f"domain must be one of {list(_domain.keys())} or an alias {list(_domain.values())}")
    else:
        domain = None

    ## Determine the Product
    if product not in _product[satellite]:
        for key, aliases in _product.items():
            if product.upper() in aliases:
                product = key
    if product not in _product[satellite]:
        raise ValueError(f"product must be one of {list(_product[satellite].keys())} or an alias {list(_product[satellite].values())}")

    return satellite, product, domain


def _goes_file_df(satellite, product, start, end, bands=None, refresh=True, ignore_missing=False):
    """Get list of requested GOES files as pandas.DataFrame.

    Parameters
    ----------
    satellite : str
    product : str
    start : datetime
    end : datetime
    band : None, int, or list
        Specify the ABI channels to retrieve.
    refresh : bool
        Refresh the s3fs.S3FileSystem object when files are listed.
        Default True will refresh and not use a cached list.
    """
    params = locals()

    start = pd.to_datetime(start)
    end = pd.to_datetime(end)

    DATES = pd.date_range(f"{start:%Y-%m-%d %H:00}", f"{end:%Y-%m-%d %H:00}", freq="1h")

    # List all files for each date
    # ----------------------------
    bucket = None
    for b in _bucket[satellite]:
        if product in _bucket_product[satellite][b]:
            bucket = b
    if bucket is None:
        raise FileNotFoundError(f"Product {product} not in any bucket ({_bucket[satellite]})")
    files = []
    for DATE in DATES:
        path = _pathspec[bucket].format(bucket=bucket,product=product,DATE=DATE)
        # path = f"{satellite}/{product}/{DATE:%Y/%j/%H/}"
        if ignore_missing is True:
            try:
                files += fs.ls(path, refresh=refresh)
            except FileNotFoundError:
                print(f"Ignored missing dir: {path}")
        else:
            files += fs.ls(path, refresh=refresh)


    # Build a table of the files
    # --------------------------
    df = pd.DataFrame(files, columns=["file"])
    df['short_file'] = df.file.str.split('/',expand=True).iloc[:,-1]
    df = df.drop_duplicates('short_file',ignore_index=True)
    if np.any(df['file'].str.contains(".nc")):
        df.drop(index=df.index[~df["file"].str.contains(".nc")],inplace=True)
    df = df.assign(**df['file'].str.extract(_namespec[bucket]))
    if 'sucode' in df.columns:
        df = df[df.sucode == 'NC']
    if 'end' not in df.columns:
        df['end'] = df['start']
    if 'creation' not in df.columns:
        df['creation'] = df['start']

    # Todo: this could use some clean up !
    if product.startswith("ABI"):
        product_mode = df.product_mode.str.rsplit("-", n=1, expand=True)
        df["product"] = product_mode[0]
        df["mode_bands"] = product_mode[1]

        mode_bands = df.mode_bands.str.split("C", expand=True)
        df["mode"] = mode_bands[0].str[1:].astype(int)
        try:
            df["band"] = mode_bands[1].astype(int)
        except Exception:  # TODO: Specify specific expected exception(s)
            # No channel data
            df["band"] = None

        # Filter files by band number
        if bands is not None:
            if not hasattr(bands, "__len__"):
                bands = [bands]
            df = df.loc[df.band.isin(bands)]

    # Filter files by requested time range
    # ------------------------------------
    # Convert filename datetime string to datetime object
    df["start"] = pd.to_datetime(df.start, format=_datespec[bucket])
    df["end"] = pd.to_datetime(df.end, format=_datespec[bucket])
    df["creation"] = pd.to_datetime(df.creation, format=_datespec[bucket])

    # Filter by files within the requested time range
    df = df.loc[df.start >= start].loc[df.end <= end].reset_index(drop=True)

    for i in params:
        df.attrs[i] = params[i]

    return df


def _download(df, save_dir, overwrite, max_threads=10, verbose=False):
    """Download the files from a DataFrame listing with multithreading."""

    def do_download(src):
        dst = Path(save_dir) / src
        if not dst.parent.is_dir():
            dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.is_file() and not overwrite:
            if verbose:
                print(f" 👮🏻‍♂️ File already exists. Do not overwrite: {dst}")
        else:
            # Downloading file from AWS
            fs.get(src, str(dst))

    ################
    # Multithreading
    tasks = len(df)
    threads = min(tasks, max_threads)

    with ThreadPoolExecutor(threads) as exe:
        futures = [exe.submit(do_download, src) for src in df.file]

        # nothing is returned in the list
        this_list = [future.result() for future in as_completed(futures)]

    print(
        f"📦 Finished downloading [{len(df)}] files to [{save_dir/Path(df.file[0]).parents[3]}]."
    )


def _as_xarray_MP(src, filetype, save_dir, i=None, n=None, verbose=True):
    """Open a file as a xarray.Dataset -- a multiprocessing helper."""
    # File destination
    local_copy = Path(save_dir) / src

    if local_copy.is_file():
        if verbose:
            print(
                f"\r📖💽 Reading ({i:,}/{n:,}) file from LOCAL COPY [{local_copy}].",
                end=" ",
            )
        ds = _reader[filetype](str(local_copy))
    #else:
    #    if verbose:
    #        print(
    #            f"\r📖☁ Reading ({i:,}/{n:,}) file from AWS to MEMORY [{src}].", end=" "
    #        )
    #    with fs.open(src, "rb") as f:
    #        ds = _reader[filetype](f)


    # Turn some attributes to coordinates so they will be preserved
    # when we concat multiple GOES DataSets together.
    """
    attr2coord = [
        "dataset_name",
        "date_created",
        "time_coverage_start",
        "time_coverage_end",
    ]
    for i in attr2coord:
        if i in ds.attrs:
            ds.coords[i] = ds.attrs.pop(i)

    ds["filename"] = src
    """
    return ds


def _as_xarray(df, **params):
    """Download files in the list to the desired path.

    Use multiprocessing to speed up the download process.

    Parameters
    ----------
    df : pandas.DataFrame
        A list of files in the GOES s3 bucket.
        This DataFrame must have a column of "files"
    params : dict
        Parameters from `goes_*` function.
    """
    params.setdefault("max_cpus", None)
    params.setdefault("verbose", True)
    save_dir = params["save_dir"]
    max_cpus = params["max_cpus"]
    verbose = params["verbose"]

    n = len(df.file)
    if n == 0:
        print("🛸 No data....🌌")
    elif n == 1:
        # If we only have one file, we don't need multiprocessing
        ds = _as_xarray_MP(df.iloc[0].file, df.iloc[0].filetype, save_dir, 1, 1, verbose)
    else:
        # Use Multiprocessing to read multiple files.
        if max_cpus is None:
            max_cpus = multiprocessing.cpu_count()
        cpus = np.minimum(multiprocessing.cpu_count(), max_cpus)
        cpus = np.minimum(cpus, n)

        inputs = [(src, filetype, save_dir, i, n) for i, (src,filetype) in enumerate(zip(df.file,df.filetype), start=1)]

        with multiprocessing.Pool(cpus) as p:
            results = p.starmap(_as_xarray_MP, inputs)
            p.close()
            p.join()

        # Need some work to concat the datasets
        #if df.attrs["product"].startswith("ABI"):
        #    print("concatenate Datasets", end="")
        #    ds = xr.concat(results, dim="t")
        #else:
        #    ds = results
        ds = results

    if verbose:
        print(f"\r{'':1000}\r📚 Finished reading [{n}] files into xarray.Dataset.")
    #ds.attrs["path"] = df.file.to_list()
    return ds


###############################################################################
###############################################################################


def goes_timerange(
    start=None,
    end=None,
    recent=None,
    *,
    satellite=config["timerange"].get("satellite"),
    product=config["timerange"].get("product"),
    domain=config["timerange"].get("domain"),
    return_as=config["timerange"].get("return_as"),
    download=config["timerange"].get("download"),
    overwrite=config["timerange"].get("overwrite"),
    save_dir=config["timerange"].get("save_dir"),
    max_cpus=config["timerange"].get("max_cpus"),
    bands=None,
    s3_refresh=config["timerange"].get("s3_refresh"),
    ignore_missing=config["timerange"].get("ignore_missing"),
    verbose=config["timerange"].get("verbose", True),
):
    """
    Get GOES data for a time range.

    Parameters
    ----------
    start, end : datetime
        Required if recent is None.
    recent : timedelta or pandas-parsable timedelta str
        Required if start and end are None. If timedelta(hours=1), will
        get the most recent files for the past hour.
    satellite : {'goes16', 'goes17', 'goes18'}
        Specify which GOES satellite.
        The following alias may also be used:

        - ``'goes16'``: 16, 'G16', or 'EAST'
        - ``'goes17'``: 17, 'G17', or 'WEST'
        - ``'goes18'``: 18, 'G18', or 'WEST'

    product : {'ABI', 'GLM', other GOES product}
        Specify the product name.

        - 'ABI' is an alias for ABI-L2-MCMIP Multichannel Cloud and Moisture Imagery
        - 'GLM' is an alias for GLM-L2-LCFA Geostationary Lightning Mapper

        Others may include ``'ABI-L1b-Rad'``, ``'ABI-L2-DMW'``, etc.
        For more available products, look at this `README
        <https://docs.opendata.aws/noaa-goes16/cics-readme.html>`_
    domain : {'C', 'F', 'M'}
        ABI scan region indicator. Only required for ABI products if the
        given product does not end with C, F, or M.

        - C: Contiguous United States (alias 'CONUS')
        - F: Full Disk (alias 'FULL')
        - M: Mesoscale (alias 'MESOSCALE')

    return_as : {'xarray', 'filelist'}
        Return the data as an xarray.Dataset or as a list of files
    download : bool
        - True: Download the data to disk to the location set by :guilabel:`save_dir`
        - False: Just load the data into memory.
    save_dir : pathlib.Path or str
        Path to save the data.
    overwrite : bool
        - True: Download the file even if it exists.
        - False Do not download the file if it already exists
    max_cpus : int
    bands : None, int, or list
        ONLY FOR L1b-Rad products; specify the bands you want
    s3_refresh : bool
        Refresh the s3fs.S3FileSystem object when files are listed.

    """
    # If `start`, or `end` is a string, parse with Pandas
    if isinstance(start, str):
        start = pd.to_datetime(start)
    if isinstance(end, str):
        end = pd.to_datetime(end)
    # If `recent` is a string (like recent='1h'), parse with Pandas
    if isinstance(recent, str):
        recent = pd.to_timedelta(recent)

    params = locals()
    satellite, product, domain = _check_param_inputs(**params)
    params["satellite"] = satellite
    params["product"] = product
    params["domain"] = domain

    check1 = start is not None and end is not None
    check2 = recent is not None
    if not (check1 or check2):
        raise ValueError("🤔 `start` and `end` *or* `recent` is required")
    if check1:
        if not (hasattr(start, "second") and hasattr(end, "second")):
            raise ValueError( "`start` and `end` must be a datetime object")
    elif check2:
        if not hasattr(recent, "seconds"):
            raise ValueError("`recent` must be a timedelta object")

    # Parameter Setup
    # ---------------
    # Create a range of directories to check. The GOES S3 bucket is
    # organized by hour of day.
    if recent is not None:
        start = datetime.utcnow() - recent
        end = datetime.utcnow()

    df = _goes_file_df(satellite, product, start, end, bands=bands, refresh=s3_refresh, ignore_missing=ignore_missing)

    if download:
        _download(df, save_dir=save_dir, overwrite=overwrite, verbose=verbose)

    if return_as == "filelist":
        df.attrs["filePath"] = save_dir
        return df
    elif return_as == "xarray":
        return _as_xarray(df, **params)

def _preprocess_single_point(ds, target_lat, target_lon, decimal_coordinates=True):
    """
    Preprocessing function to select only the single relevant data subset.

    Parameters
    ----------
    ds: xarray Dataset
        The dataset to look through and choose the particular location
    target_lat, target_lon : float
        Location where you wish to extract the point values from
    decimal_coordinates: bool
        If latitude/longitude are specified in decimal or radian coordinates.
    """
    x_target, y_target = lat_lon_to_scan_angles(target_lat, target_lon, ds["goes_imager_projection"], decimal_coordinates)
    return ds.sel(x=x_target, y=y_target, method="nearest")

def goes_single_point_timerange(
    latitude,
    longitude,
    start=None,
    end=None,
    recent=None,
    decimal_coordinates=True,
    *,
    satellite=config["timerange"].get("satellite"),
    product=config["timerange"].get("product"),
    domain=config["timerange"].get("domain"),
    return_as=config["timerange"].get("return_as"),
    download=config["timerange"].get("download"),
    overwrite=config["timerange"].get("overwrite"),
    save_dir=config["timerange"].get("save_dir"),
    max_cpus=config["timerange"].get("max_cpus"),
    bands=None,
    s3_refresh=config["timerange"].get("s3_refresh"),
    ignore_missing=config["timerange"].get("ignore_missing"),
    verbose=config["timerange"].get("verbose", True),
):
    """
    Get GOES data for a time range at the scan point nearest to a defined single latitude/longitude point.

    Parameters
    ----------
    latitude, longitude : float
            Location where you wish to extract the point values from
    start, end : datetime
        Required if recent is None.
    recent : timedelta or pandas-parsable timedelta str
        Required if start and end are None. If timedelta(hours=1), will
        get the most recent files for the past hour.
    decimal_coordinates: bool
        If latitude/longitude are specified in decimal or radian coordinates.
    satellite : {'goes16', 'goes17', 'goes18'}
        Specify which GOES satellite.
        The following alias may also be used:

        - ``'goes16'``: 16, 'G16', or 'EAST'
        - ``'goes17'``: 17, 'G17', or 'WEST'
        - ``'goes18'``: 18, 'G18', or 'WEST'

    product : {'ABI', 'GLM', other GOES product}
        Specify the product name.

        - 'ABI' is an alias for ABI-L2-MCMIP Multichannel Cloud and Moisture Imagery
        - 'GLM' is an alias for GLM-L2-LCFA Geostationary Lightning Mapper

        Others may include ``'ABI-L1b-Rad'``, ``'ABI-L2-DMW'``, etc.
        For more available products, look at this `README
        <https://docs.opendata.aws/noaa-goes16/cics-readme.html>`_
    domain : {'C', 'F', 'M'}
        ABI scan region indicator. Only required for ABI products if the
        given product does not end with C, F, or M.

        - C: Contiguous United States (alias 'CONUS')
        - F: Full Disk (alias 'FULL')
        - M: Mesoscale (alias 'MESOSCALE')

    return_as : {'xarray', 'filelist'}
        Return the data as an xarray.Dataset or as a list of files
    download : bool
        - True: Download the data to disk to the location set by :guilabel:`save_dir`
        - False: Just load the data into memory.
    save_dir : pathlib.Path or str
        Path to save the data.
    overwrite : bool
        - True: Download the file even if it exists.
        - False Do not download the file if it already exists
    max_cpus : int
    bands : None, int, or list
        ONLY FOR L1b-Rad products; specify the bands you want
    s3_refresh : bool
        Refresh the s3fs.S3FileSystem object when files are listed.

    """
    # If `start`, or `end` is a string, parse with Pandas
    if isinstance(start, str):
        start = pd.to_datetime(start)
    if isinstance(end, str):
        end = pd.to_datetime(end)
    # If `recent` is a string (like recent='1h'), parse with Pandas
    if isinstance(recent, str):
        recent = pd.to_timedelta(recent)

    params = locals()
    satellite, product, domain = _check_param_inputs(**params)
    params["satellite"] = satellite
    params["product"] = product
    params["domain"] = domain

    check1 = start is not None and end is not None
    check2 = recent is not None
    if not (check1 or check2):
        raise ValueError("🤔 `start` and `end` *or* `recent` is required")
    if check1:
        if not (hasattr(start, "second") and hasattr(end, "second")):
            raise ValueError( "`start` and `end` must be a datetime object")
    elif check2:
        if not hasattr(recent, "seconds"):
            raise ValueError("`recent` must be a timedelta object")

    # Parameter Setup
    # ---------------
    # Create a range of directories to check. The GOES S3 bucket is
    # organized by hour of day.
    if recent is not None:
        start = datetime.utcnow() - recent
        end = datetime.utcnow()

    df = _goes_file_df(satellite, product, start, end, bands=bands, refresh=s3_refresh, ignore_missing=ignore_missing)

    if download:
        _download(df, save_dir=save_dir, overwrite=overwrite, verbose=verbose)

    if return_as == "filelist":
        df.attrs["filePath"] = save_dir
        return df
    elif return_as == "xarray":
        partial_func = partial(_preprocess_single_point, target_lat=latitude, target_lon=longitude, decimal_coordinates=decimal_coordinates)
        preprocessed_ds = xr.open_mfdataset([str(config['timerange']['save_dir']) + "/" + f for f in df['file'].to_list()],
                  concat_dim='t',
                  combine='nested',
                  preprocess=partial_func)
        return preprocessed_ds


def goes_latest(
    *,
    satellite=config["latest"].get("satellite"),
    product=config["latest"].get("product"),
    domain=config["latest"].get("domain"),
    return_as=config["latest"].get("return_as"),
    download=config["latest"].get("download"),
    overwrite=config["latest"].get("overwrite"),
    save_dir=config["latest"].get("save_dir"),
    bands=None,
    s3_refresh=config["latest"].get("s3_refresh"),
    ignore_missing=config["latest"].get("ignore_missing"),
    verbose=config["latest"].get("verbose", True),
):
    """
    Get the latest available GOES data.

    Parameters
    ----------
    satellite : {'goes16', 'goes17', 'goes18'}
        Specify which GOES satellite.
        The following alias may also be used:

        - ``'goes16'``: 16, 'G16', or 'EAST'
        - ``'goes17'``: 17, 'G17', or 'WEST'
        - ``'goes18'``: 18, 'G18', or 'WEST'

    product : {'ABI', 'GLM', other GOES product}
        Specify the product name.

        - 'ABI' is an alias for ABI-L2-MCMIP Multichannel Cloud and Moisture Imagery
        - 'GLM' is an alias for GLM-L2-LCFA Geostationary Lightning Mapper

        Others may include ``'ABI-L1b-Rad'``, ``'ABI-L2-DMW'``, etc.
        For more available products, look at this `README
        <https://docs.opendata.aws/noaa-goes16/cics-readme.html>`_
    domain : {'C', 'F', 'M'}
        ABI scan region indicator. Only required for ABI products if the
        given product does not end with C, F, or M.

        - C: Contiguous United States (alias 'CONUS')
        - F: Full Disk (alias 'FULL')
        - M: Mesoscale (alias 'MESOSCALE')

    return_as : {'xarray', 'filelist'}
        Return the data as an xarray.Dataset or as a list of files
    download : bool
        - True: Download the data to disk to the location set by :guilabel:`save_dir`
        - False: Just load the data into memory.
    save_dir : pathlib.Path or str
        Path to save the data.
    overwrite : bool
        - True: Download the file even if it exists.
        - False Do not download the file if it already exists
    bands : None, int, or list
        ONLY FOR L1b-Rad products; specify the bands you want
    s3_refresh : bool
        Refresh the s3fs.S3FileSystem object when files are listed.
    """
    params = locals()
    satellite, product, domain = _check_param_inputs(**params)
    params["satellite"] = satellite
    params["product"] = product
    params["domain"] = domain

    # Parameter Setup
    # ---------------
    # Create a range of directories to check. The GOES S3 bucket is
    # organized by hour of day. Look in the current hour and last hour.
    start = datetime.utcnow() - timedelta(hours=1)
    end = datetime.utcnow()

    df = _goes_file_df(satellite, product, start, end, bands=bands, refresh=s3_refresh, ignore_missing=ignore_missing)

    # Filter for specific mesoscale domain
    if domain is not None and domain.upper() in ["M1", "M2"]:
        df = df[df["file"].str.contains(f"{domain.upper()}-M")]

    # Get the most recent file (latest start date)
    df = df.loc[df.start == df.start.max()].reset_index(drop=True)

    if download:
        _download(df, save_dir=save_dir, overwrite=overwrite, verbose=verbose)

    if return_as == "filelist":
        df.attrs["filePath"] = save_dir
        return df
    elif return_as == "xarray":
        return _as_xarray(df, **params)


def goes_nearesttime(
    attime,
    within=pd.to_timedelta(config["nearesttime"].get("within", "1h")),
    *,
    satellite=config["nearesttime"].get("satellite"),
    product=config["nearesttime"].get("product"),
    domain=config["nearesttime"].get("domain"),
    return_as=config["nearesttime"].get("return_as"),
    download=config["nearesttime"].get("download"),
    overwrite=config["nearesttime"].get("overwrite"),
    save_dir=config["nearesttime"].get("save_dir"),
    bands=None,
    s3_refresh=config["nearesttime"].get("s3_refresh"),
    ignore_missing=config["nearesttime"].get("ignore_missing"),
    verbose=config["nearesttime"].get("verbose", True),
):
    """
    Get the GOES data nearest a specified time.

    Parameters
    ----------
    attime : datetime
        Time to find the nearest observation for.
        May also use a pandas-interpretable datetime string.
    within : timedelta or pandas-parsable timedelta str
        Timerange tht the nearest observation must be.
    satellite : {'goes16', 'goes17', 'goes18'}
        Specify which GOES satellite.
        The following alias may also be used:

        - ``'goes16'``: 16, 'G16', or 'EAST'
        - ``'goes17'``: 17, 'G17', or 'WEST'
        - ``'goes18'``: 18, 'G18', or 'WEST'

    product : {'ABI', 'GLM', other GOES product}
        Specify the product name.

        - 'ABI' is an alias for ABI-L2-MCMIP Multichannel Cloud and Moisture Imagery
        - 'GLM' is an alias for GLM-L2-LCFA Geostationary Lightning Mapper

        Others may include ``'ABI-L1b-Rad'``, ``'ABI-L2-DMW'``, etc.
        For more available products, look at this `README
        <https://docs.opendata.aws/noaa-goes16/cics-readme.html>`_
    domain : {'C', 'F', 'M'}
        ABI scan region indicator. Only required for ABI products if the
        given product does not end with C, F, or M.

        - C: Contiguous United States (alias 'CONUS')
        - F: Full Disk (alias 'FULL')
        - M: Mesoscale (alias 'MESOSCALE')

    return_as : {'xarray', 'filelist'}
        Return the data as an xarray.Dataset or as a list of files
    download : bool
        - True: Download the data to disk to the location set by :guilabel:`save_dir`
        - False: Just load the data into memory.
    save_dir : pathlib.Path or str
        Path to save the data.
    overwrite : bool
        - True: Download the file even if it exists.
        - False: Do not download the file if it already exists
    bands : None, int, or list
        ONLY FOR L1b-Rad products; specify the bands you want
    s3_refresh : bool
        Refresh the s3fs.S3FileSystem object when files are listed.
    """
    if isinstance(attime, str):
        attime = pd.to_datetime(attime)
    if isinstance(within, str):
        within = pd.to_timedelta(within)

    params = locals()
    satellite, product, _ = _check_param_inputs(**params)
    params["satellite"] = satellite
    params["product"] = product

    # Parameter Setup
    # ---------------
    # Create a range of directories to check. The GOES S3 bucket is
    # organized by hour of day.
    start = attime - within
    end = attime + within

    df = _goes_file_df(satellite, product, start, end, bands=bands, refresh=s3_refresh, ignore_missing=ignore_missing)
    # return df, start, end, attime

    # Filter for specific mesoscale domain
    if domain.upper() in ["M1", "M2"]:
        df = df[df["file"].str.contains(f"{domain.upper()}-M")]

    # Get row that matches the nearest time
    df['file_short'] = df.file.str.split('/',expand=True).iloc[:,-1]
    df = df.sort_values("start")
    df = df.set_index(df.start,drop=True)
    unique_times_index = df.index.unique()
    nearest_time_index = unique_times_index.get_indexer([attime], method="nearest")
    nearest_time = unique_times_index[nearest_time_index]
    df = df.loc[nearest_time]

    df = df.reset_index(drop=True)

    n = len(df.file)
    if n == 0:
        print("🛸 No data....🌌")
        return None

    if download:
        _download(df, save_dir=save_dir, overwrite=overwrite, verbose=verbose)

    if return_as == "filelist":
        df.attrs["filePath"] = save_dir
        return df
    elif return_as == "xarray":
        return _as_xarray(df, **params)
