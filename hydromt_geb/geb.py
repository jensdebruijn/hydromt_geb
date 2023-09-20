from tqdm import tqdm
from pathlib import Path
from typing import List, Optional
from hydromt.models.model_grid import GridMixin, GridModel
import hydromt.workflows
from dateutil.relativedelta import relativedelta
import logging
import os
import math
import requests
import time
import random
import zipfile
import json
import numpy as np
import pandas as pd
import xarray as xr
import rioxarray as rxr
from urllib.parse import urlparse
from dask.diagnostics import ProgressBar
from typing import Union, Any, Dict

import xclim.indices as xci
from scipy.stats import genextreme

# temporary fix for ESMF on Windows
if os.name == 'nt':
    os.environ['ESMFMKFILE'] = str(Path(os.__file__).parent.parent / 'Library' / 'lib' / 'esmf.mk')

import xesmf as xe
from affine import Affine
import geopandas as gpd
from datetime import date, datetime
from calendar import monthrange
from isimip_client.client import ISIMIPClient

from .workflows import repeat_grid, clip_with_grid, get_modflow_transform_and_shape, create_indices, create_modflow_basin, pad_xy, create_farms, get_farm_distribution, calculate_cell_area

logger = logging.getLogger(__name__)

class GEBModel(GridModel):
    _CLI_ARGS = {"region": "setup_grid"}
    
    def __init__(
        self,
        root: str = None,
        mode: str = "w",
        config_fn: str = None,
        data_libs: List[str] = None,
        logger=logger,
        epsg=4326
    ):
        """Initialize a GridModel for distributed models with a regular grid."""
        super().__init__(
            root=root,
            mode=mode,
            config_fn=config_fn,
            data_libs=data_libs,
            logger=logger,
        )

        self.epsg = epsg
        
        self.subgrid = GridMixin()
        # TODO: How to do this properly?
        self.subgrid._read = True
        self.subgrid.logger = self.logger
        self.region_subgrid = GridMixin()
        self.region_subgrid._read = True
        self.region_subgrid.logger = self.logger
        self.MERIT_grid = GridMixin()
        self.MERIT_grid._read = True
        self.MERIT_grid.logger = self.logger
        self.MODFLOW_grid = GridMixin()
        self.MODFLOW_grid._read = True
        self.MODFLOW_grid.logger = self.logger
        self.table = {}
        self.binary = {}
        self.dict = {}

        self.model_structure = {}

    def setup_grid(
        self,
        region: dict,
        sub_grid_factor: int,
        hydrography_fn: str,
        basin_index_fn: str,
    ) -> xr.DataArray:
        """Creates a 2D regular grid or reads an existing grid.
        An 2D regular grid will be created from a geometry (geom_fn) or bbox. If an existing
        grid is given, then no new grid will be generated.

        Adds/Updates model layers:
        * **grid** grid mask: add grid mask to grid object

        Parameters
        ----------
        region : dict
            Dictionary describing region of interest, e.g.:
            * {'basin': [x, y]}

            Region must be of kind [basin, subbasin].
        sub_grid_factor : int
            GEB implements a subgrid. This parameter determines the factor by which the subgrid is smaller than the original grid.
        hydrography_fn : str
            Name of data source for hydrography data.
        basin_index_fn : str
            Name of data source with basin (bounding box) geometries associated with
            the 'basins' layer of `hydrography_fn`.
        """

        assert sub_grid_factor > 10, "sub_grid_factor must be larger than 10, because this is the resolution of the MERIT high-res DEM"
        assert sub_grid_factor % 10 == 0, "sub_grid_factor must be a multiple of 10"
        self.subgrid_factor = sub_grid_factor

        self.logger.info(f"Preparing 2D grid.")
        kind, region = hydromt.workflows.parse_region(region, logger=self.logger)
        if kind in ["basin", "subbasin"]:
            # retrieve global hydrography data (lazy!)
            ds_org = self.data_catalog.get_rasterdataset(hydrography_fn)
            if "bounds" not in region:
                region.update(basin_index=self.data_catalog[basin_index_fn])
            # get basin geometry
            geom, xy = hydromt.workflows.get_basin_geometry(
                ds=ds_org,
                kind=kind,
                logger=self.logger,
                **region
            )
            region.update(xy=xy)
            ds_hydro = ds_org.raster.clip_geom(geom, mask=True)
        else:
            raise ValueError(
                f"Region for grid must of kind [basin, subbasin], kind {kind} not understood."
            )

        # Add region and grid to model
        self.set_geoms(geom, name="areamaps/region")
        
        ldd = ds_hydro['flwdir'].raster.reclassify(
            reclass_table=pd.DataFrame(
                index=[0, 1, 2, 4, 8, 16, 32, 64, 128, ds_hydro['flwdir'].raster.nodata],
                data={"ldd": [5, 6, 3, 2, 1, 4, 7, 8, 9, 0]}
            ),
            method="exact"
        )['ldd']
        
        self.set_grid(ldd, name='routing/kinematic/ldd')
        self.set_grid(ds_hydro['uparea'], name='routing/kinematic/upstream_area')
        self.set_grid(ds_hydro['elevtn'], name='landsurface/topo/elevation')
        self.set_grid(
            xr.where(ds_hydro['rivlen_ds'] != -9999, ds_hydro['rivlen_ds'], np.nan, keep_attrs=True),
            name='routing/kinematic/channel_length'
        )
        self.set_grid(ds_hydro['rivslp'], name='routing/kinematic/channel_slope')
        
        # ds_hydro['mask'].raster.set_nodata(-1)
        self.set_grid((~ds_hydro['mask']).astype(np.int8), name='areamaps/grid_mask')

        mask = self.grid['areamaps/grid_mask']

        dst_transform = mask.raster.transform * Affine.scale(1 / sub_grid_factor)

        submask = hydromt.raster.full_from_transform(
            dst_transform,
            (mask.raster.shape[0] * sub_grid_factor, mask.raster.shape[1] * sub_grid_factor), 
            nodata=0,
            dtype=mask.dtype,
            crs=mask.raster.crs,
            name='areamaps/sub_grid_mask',
            lazy=True
        )
        submask.raster.set_nodata(None)
        submask.data = repeat_grid(mask.data, sub_grid_factor)

        self.subgrid.set_grid(submask)
        self.subgrid.factor = sub_grid_factor

    def setup_cell_area_map(self) -> None:
        """
        Sets up the cell area map for the model.

        Raises
        ------
        ValueError
            If the grid mask is not available.

        Notes
        -----
        This method prepares the cell area map for the model by calculating the area of each cell in the grid. It first
        retrieves the grid mask from the `areamaps/grid_mask` attribute of the grid, and then calculates the cell area
        using the `calculate_cell_area()` function. The resulting cell area map is then set as the `areamaps/cell_area`
        attribute of the grid.

        Additionally, this method sets up a subgrid for the cell area map by creating a new grid with the same extent as
        the subgrid, and then repeating the cell area values from the main grid to the subgrid using the `repeat_grid()`
        function, and correcting for the subgrid factor. Thus, every subgrid cell within a grid cell has the same value.
        The resulting subgrid cell area map is then set as the `areamaps/sub_cell_area` attribute of the subgrid.
        """
        self.logger.info(f"Preparing cell area map.")
        mask = self.grid['areamaps/grid_mask'].raster
        affine = mask.transform

        cell_area = hydromt.raster.full(mask.coords, nodata=np.nan, dtype=np.float32, name='areamaps/cell_area', lazy=True)
        cell_area.data = calculate_cell_area(affine, mask.shape)
        self.set_grid(cell_area)

        sub_cell_area = hydromt.raster.full(
            self.subgrid.grid.raster.coords,
            nodata=cell_area.raster.nodata,
            dtype=cell_area.dtype,
            name='areamaps/sub_cell_area',
            lazy=True
        )

        sub_cell_area.data = repeat_grid(cell_area.data, self.subgrid.factor) / self.subgrid.factor ** 2
        self.subgrid.set_grid(sub_cell_area)

    def setup_crops(
            self,
            crop_ids: dict,
            crop_variables: dict,
            crop_prices: Optional[Union[str, Dict[str, Any]]] = None,
            cultivation_costs: Optional[Union[str, Dict[str, Any]]] = None,
        ):
        """
        Sets up the crops data for the model.

        Parameters
        ----------
        crop_ids : dict
            A dictionary of crop IDs and names.
        crop_variables : dict
            A dictionary of crop variables and their values.
        crop_prices : str or dict, optional
            The file path or dictionary of crop prices. If a file path is provided, the file is loaded and parsed as JSON.
            The dictionary should have a 'time' key with a list of time steps, and a 'crops' key with a dictionary of crop
            IDs and their prices.
        cultivation_costs : str or dict, optional
            The file path or dictionary of cultivation costs. If a file path is provided, the file is loaded and parsed as
            JSON. The dictionary should have a 'time' key with a list of time steps, and a 'crops' key with a dictionary of
            crop IDs and their cultivation costs.
        """
        self.logger.info(f"Preparing crops data")
        self.set_dict(crop_ids, name='crops/crop_ids')
        self.set_dict(crop_variables, name='crops/crop_variables')
        if crop_prices is not None:
            self.logger.info(f"Preparing crop prices")
            if isinstance(crop_prices, str):
                fp = Path(self.root, crop_prices)
                if not fp.exists():
                    raise ValueError(f"crop_prices file {fp.resolve()} does not exist")
                with open(fp, 'r') as f:
                    crop_prices_data = json.load(f)
                crop_prices = {
                    'time': crop_prices_data['time'],
                    'crops': {
                        crop_id: crop_prices_data['crops'][crop_name]
                        for crop_id, crop_name in crop_ids.items()
                    }
                }
            self.set_dict(crop_prices, name='crops/crop_prices')
        if cultivation_costs is not None:
            self.logger.info(f"Preparing cultivation costs")
            if isinstance(cultivation_costs, str):
                fp = Path(self.root, cultivation_costs)
                if not fp.exists():
                    raise ValueError(f"cultivation_costs file {fp.resolve()} does not exist")
                with open(fp) as f:
                    cultivation_costs = json.load(f)
                cultivation_costs = {
                    'time': cultivation_costs['time'],
                    'crops': {
                        crop_id: cultivation_costs['crops'][crop_name]
                        for crop_id, crop_name in crop_ids.items()
                    }
                }
            self.set_dict(cultivation_costs, name='crops/cultivation_costs')

    def setup_mannings(self) -> None:
        """
        Sets up the Manning's coefficient for the model.

        Notes
        -----
        This method sets up the Manning's coefficient for the model by calculating the coefficient based on the cell area
        and topography of the grid. It first calculates the upstream area of each cell in the grid using the
        `routing/kinematic/upstream_area` attribute of the grid. It then calculates the coefficient using the formula:

            C = 0.025 + 0.015 * (2 * A / U) + 0.030 * (Z / 2000)

        where C is the Manning's coefficient, A is the cell area, U is the upstream area, and Z is the elevation of the cell.

        The resulting Manning's coefficient is then set as the `routing/kinematic/mannings` attribute of the grid using the
        `set_grid()` method.
        """
        self.logger.info("Setting up Manning's coefficient")
        a = (2 * self.grid['areamaps/cell_area']) / self.grid['routing/kinematic/upstream_area']
        a = xr.where(a > 1, 1, a)
        b = self.grid['landsurface/topo/elevation'] / 2000
        b = xr.where(b > 1, 1, b)
        
        mannings = hydromt.raster.full(self.grid.raster.coords, nodata=np.nan, dtype=np.float32, name='routing/kinematic/mannings', lazy=True)
        mannings.data = 0.025 + 0.015 * a + 0.030 * b
        self.set_grid(mannings)

    def setup_channel_width(self, minimum_width: float) -> None:
        """
        Sets up the channel width for the model.

        Parameters
        ----------
        minimum_width : float
            The minimum channel width in meters.

        Notes
        -----
        This method sets up the channel width for the model by calculating the width of each channel based on the upstream
        area of each cell in the grid. It first retrieves the upstream area of each cell from the `routing/kinematic/upstream_area`
        attribute of the grid, and then calculates the channel width using the formula:

            W = A / 500

        where W is the channel width, and A is the upstream area of the cell. The resulting channel width is then set as
        the `routing/kinematic/channel_width` attribute of the grid using the `set_grid()` method.

        Additionally, this method sets a minimum channel width by replacing any channel width values that are less than the
        minimum width with the minimum width.
        """
        self.logger.info("Setting up channel width")
        channel_width_data = self.grid['routing/kinematic/upstream_area'] / 500
        channel_width_data = xr.where(channel_width_data < minimum_width, minimum_width, channel_width_data)
        
        channel_width = hydromt.raster.full(self.grid.raster.coords, nodata=np.nan, dtype=np.float32, name='routing/kinematic/channel_width', lazy=True)
        channel_width.data = channel_width_data
        
        self.set_grid(channel_width)

    def setup_channel_depth(self) -> None:
        """
        Sets up the channel depth for the model.

        Raises
        ------
        AssertionError
            If the upstream area of any cell in the grid is less than or equal to zero.

        Notes
        -----
        This method sets up the channel depth for the model by calculating the depth of each channel based on the upstream
        area of each cell in the grid. It first retrieves the upstream area of each cell from the `routing/kinematic/upstream_area`
        attribute of the grid, and then calculates the channel depth using the formula:

            D = 0.27 * A ** 0.26

        where D is the channel depth, and A is the upstream area of the cell. The resulting channel depth is then set as
        the `routing/kinematic/channel_depth` attribute of the grid using the `set_grid()` method.

        Additionally, this method raises an `AssertionError` if the upstream area of any cell in the grid is less than or
        equal to zero. This is done to ensure that the upstream area is a positive value, which is required for the channel
        depth calculation to be valid.
        """
        self.logger.info("Setting up channel depth")
        assert ((self.grid['routing/kinematic/upstream_area'] > 0) | ~self.grid.mask).all()
        channel_depth_data = 0.27 * self.grid['routing/kinematic/upstream_area'] ** 0.26
        channel_depth = hydromt.raster.full(self.grid.raster.coords, nodata=np.nan, dtype=np.float32, name='routing/kinematic/channel_depth', lazy=True)
        channel_depth.data = channel_depth_data
        self.set_grid(channel_depth)

    def setup_channel_ratio(self) -> None:
        """
        Sets up the channel ratio for the model.

        Raises
        ------
        AssertionError
            If the channel length of any cell in the grid is less than or equal to zero, or if the channel ratio of any
            cell in the grid is less than zero.

        Notes
        -----
        This method sets up the channel ratio for the model by calculating the ratio of the channel area to the cell area
        for each cell in the grid. It first retrieves the channel width and length from the `routing/kinematic/channel_width`
        and `routing/kinematic/channel_length` attributes of the grid, and then calculates the channel area using the
        product of the width and length. It then calculates the channel ratio by dividing the channel area by the cell area
        retrieved from the `areamaps/cell_area` attribute of the grid.

        The resulting channel ratio is then set as the `routing/kinematic/channel_ratio` attribute of the grid using the
        `set_grid()` method. Any channel ratio values that are greater than 1 are replaced with 1 (i.e., the whole cell is a channel).

        Additionally, this method raises an `AssertionError` if the channel length of any cell in the grid is less than or
        equal to zero, or if the channel ratio of any cell in the grid is less than zero. These checks are done to ensure
        that the channel length and ratio are positive values, which are required for the channel ratio calculation to be
        valid.
        """
        self.logger.info("Setting up channel ratio")
        assert ((self.grid['routing/kinematic/channel_length'] > 0) | ~self.grid.mask).all()
        channel_area = self.grid['routing/kinematic/channel_width'] * self.grid['routing/kinematic/channel_length']
        channel_ratio_data = channel_area / self.grid['areamaps/cell_area']
        channel_ratio_data = xr.where(channel_ratio_data > 1, 1, channel_ratio_data)
        assert ((channel_ratio_data >= 0) | ~self.grid.mask).all()
        channel_ratio = hydromt.raster.full(self.grid.raster.coords, nodata=np.nan, dtype=np.float32, name='routing/kinematic/channel_ratio', lazy=True)
        channel_ratio.data = channel_ratio_data
        self.set_grid(channel_ratio)

    def setup_elevation_STD(self) -> None:
        """
        Sets up the standard deviation of elevation for the model.
        
        Notes
        -----
        This method sets up the standard deviation of elevation for the model by retrieving high-resolution elevation data
        from the MERIT dataset and calculating the standard deviation of elevation for each cell in the grid. 
        
        MERIT data has a half cell offset. Therefore, this function first corrects for this offset.  It then selects the
        high-resolution elevation data from the MERIT dataset using the grid coordinates of the model, and calculates the
        standard deviation of elevation for each cell in the grid using the `np.std()` function.

        The resulting standard deviation of elevation is then set as the `landsurface/topo/elevation_STD` attribute of
        the grid using the `set_grid()` method.
        """
        self.logger.info("Setting up elevation standard deviation")
        MERIT = self.data_catalog.get_rasterdataset("merit_hydro", variables=['elv'])
        # There is a half degree offset in MERIT data
        MERIT = MERIT.assign_coords(
            x=MERIT.coords['x'] + MERIT.rio.resolution()[0] / 2,
            y=MERIT.coords['y'] - MERIT.rio.resolution()[1] / 2
        )

        # we are going to match the upper left corners. So create a MERIT grid with the upper left corners as coordinates
        MERIT_ul = MERIT.assign_coords(
            x=MERIT.coords['x'] - MERIT.rio.resolution()[0] / 2,
            y=MERIT.coords['y'] - MERIT.rio.resolution()[1] / 2
        )

        scaling = 10

        # find the upper left corner of the grid cells in self.grid
        y_step = self.grid.get_index('y')[1] - self.grid.get_index('y')[0]
        x_step = self.grid.get_index('x')[1] - self.grid.get_index('x')[0]
        upper_left_y = self.grid.get_index('y')[0] - y_step / 2
        upper_left_x = self.grid.get_index('x')[0] - x_step / 2
        
        ymin = np.isclose(MERIT_ul.get_index('y'), upper_left_y, atol=MERIT.rio.resolution()[1] / 100)
        assert ymin.sum() == 1, "Could not find the upper left corner of the grid cell in MERIT data"
        ymin = ymin.argmax()
        ymax = ymin + self.grid.mask.shape[0] * scaling
        xmin = np.isclose(MERIT_ul.get_index('x'), upper_left_x, atol=MERIT.rio.resolution()[0] / 100)
        assert xmin.sum() == 1, "Could not find the upper left corner of the grid cell in MERIT data"
        xmin = xmin.argmax()
        xmax = xmin + self.grid.mask.shape[1] * scaling

        # select data from MERIT using the grid coordinates
        high_res_elevation_data = MERIT.isel(
            y=slice(ymin, ymax),
            x=slice(xmin, xmax)
        )

        self.MERIT_grid.set_grid(MERIT.isel(
            y=slice(ymin-1, ymax+1),
            x=slice(xmin-1, xmax+1)
        ), name='landsurface/topo/subgrid_elevation')

        elevation_per_cell = (
            high_res_elevation_data.values.reshape(high_res_elevation_data.shape[0] // scaling, scaling, -1, scaling
        ).swapaxes(1, 2).reshape(-1, scaling, scaling))

        elevation_per_cell = high_res_elevation_data.values.reshape(high_res_elevation_data.shape[0] // scaling, scaling, -1, scaling).swapaxes(1, 2)

        standard_deviation = hydromt.raster.full(self.grid.raster.coords, nodata=np.nan, dtype=np.float32, name='landsurface/topo/elevation_STD', lazy=True)
        standard_deviation.data = np.std(elevation_per_cell, axis=(2,3))
        self.set_grid(standard_deviation)

    def setup_soil_parameters(self, interpolation_method='nearest') -> None:
        """
        Sets up the soil parameters for the model.

        Parameters
        ----------
        interpolation_method : str, optional
            The interpolation method to use when interpolating the soil parameters. Default is 'nearest'.

        Notes
        -----
        This method sets up the soil parameters for the model by retrieving soil data from the CWATM dataset and interpolating
        the data to the model grid. It first retrieves the soil dataset from the `data_catalog`, and
        then retrieves the soil parameters and storage depth data for each soil layer. It then interpolates the data to the
        model grid using the specified interpolation method and sets the resulting grids as attributes of the model.

        Additionally, this method sets up the percolation impeded and crop group data by retrieving the corresponding data
        from the soil dataset and interpolating it to the model grid.

        The resulting soil parameters are set as attributes of the model with names of the form 'soil/{parameter}{soil_layer}',
        where {parameter} is the name of the soil parameter (e.g. 'alpha', 'ksat', etc.) and {soil_layer} is the index of the
        soil layer (1-3; 1 is the top layer). The storage depth data is set as attributes of the model with names of the
        form 'soil/storage_depth{soil_layer}'. The percolation impeded and crop group data are set as attributes of the model
        with names 'soil/percolation_impeded' and 'soil/cropgrp', respectively.
        """
        self.logger.info('Setting up soil parameters')
        soil_ds = self.data_catalog.get_rasterdataset("cwatm_soil_5min")
        for parameter in ('alpha', 'ksat', 'lambda', 'thetar', 'thetas'):
            for soil_layer in range(1, 4):
                ds = soil_ds[f'{parameter}{soil_layer}_5min']
                self.set_grid(self.interpolate(ds, interpolation_method), name=f'soil/{parameter}{soil_layer}')

        for soil_layer in range(1, 3):
            ds = soil_ds[f'storageDepth{soil_layer}']
            self.set_grid(self.interpolate(ds, interpolation_method), name=f'soil/storage_depth{soil_layer}')

        ds = soil_ds['percolationImp']
        self.set_grid(self.interpolate(ds, interpolation_method), name=f'soil/percolation_impeded')
        ds = soil_ds['cropgrp']
        self.set_grid(self.interpolate(ds, interpolation_method), name=f'soil/cropgrp')

    def setup_land_use_parameters(self, interpolation_method='nearest') -> None:
        """
        Sets up the land use parameters for the model.

        Parameters
        ----------
        interpolation_method : str, optional
            The interpolation method to use when interpolating the land use parameters. Default is 'nearest'.

        Notes
        -----
        This method sets up the land use parameters for the model by retrieving land use data from the CWATM dataset and
        interpolating the data to the model grid. It first retrieves the land use dataset from the `data_catalog`, and 
        then retrieves the maximum root depth and root fraction data for each land use type. It then
        interpolates the data to the model grid using the specified interpolation method and sets the resulting grids as
        attributes of the model with names of the form 'landcover/{land_use_type}/{parameter}_{land_use_type}', where
        {land_use_type} is the name of the land use type (e.g. 'forest', 'grassland', etc.) and {parameter} is the name of
        the land use parameter (e.g. 'maxRootDepth', 'rootFraction1', etc.).

        Additionally, this method sets up the crop coefficient and interception capacity data for each land use type by
        retrieving the corresponding data from the land use dataset and interpolating it to the model grid. The crop
        coefficient data is set as attributes of the model with names of the form 'landcover/{land_use_type}/cropCoefficient{land_use_type_netcdf_name}_10days',
        where {land_use_type_netcdf_name} is the name of the land use type in the CWATM dataset. The interception capacity
        data is set as attributes of the model with names of the form 'landcover/{land_use_type}/interceptCap{land_use_type_netcdf_name}_10days',
        where {land_use_type_netcdf_name} is the name of the land use type in the CWATM dataset.

        The resulting land use parameters are set as attributes of the model with names of the form 'landcover/{land_use_type}/{parameter}_{land_use_type}',
        where {land_use_type} is the name of the land use type (e.g. 'forest', 'grassland', etc.) and {parameter} is the name of
        the land use parameter (e.g. 'maxRootDepth', 'rootFraction1', etc.). The crop coefficient data is set as attributes
        of the model with names of the form 'landcover/{land_use_type}/cropCoefficient{land_use_type_netcdf_name}_10days',
        where {land_use_type_netcdf_name} is the name of the land use type in the CWATM dataset. The interception capacity
        data is set as attributes of the model with names of the form 'landcover/{land_use_type}/interceptCap{land_use_type_netcdf_name}_10days',
        where {land_use_type_netcdf_name} is the name of the land use type in the CWATM dataset.
        """
        self.logger.info('Setting up land use parameters')
        for land_use_type, land_use_type_netcdf_name in (
            ('forest', 'Forest'),
            ('grassland', 'Grassland'),
            ('irrPaddy', 'irrPaddy'),
            ('irrNonPaddy', 'irrNonPaddy'),
        ):
            self.logger.info(f'Setting up land use parameters for {land_use_type}')
            land_use_ds = self.data_catalog.get_rasterdataset(f"cwatm_{land_use_type}_5min")
            
            for parameter in ('maxRootDepth', 'rootFraction1'):
                self.set_grid(
                    self.interpolate(land_use_ds[parameter], interpolation_method),
                    name=f'landcover/{land_use_type}/{parameter}_{land_use_type}'
                )
            
            parameter = f'cropCoefficient{land_use_type_netcdf_name}_10days'               
            self.set_forcing(
                self.interpolate(land_use_ds[parameter], interpolation_method),
                name=f'landcover/{land_use_type}/{parameter}'
            )
            if land_use_type in ('forest', 'grassland'):
                parameter = f'interceptCap{land_use_type_netcdf_name}_10days'               
                self.set_forcing(
                    self.interpolate(land_use_ds[parameter], interpolation_method),
                    name=f'landcover/{land_use_type}/{parameter}'
                )

    def setup_waterbodies(self):
        """
        Sets up the waterbodies for GEB.

        Notes
        -----
        This method sets up the waterbodies for GEB. It first retrieves the waterbody data from the
        specified data catalog and sets it as a geometry in the model. It then rasterizes the waterbody data onto the model
        grid and the subgrid using the `rasterize` method of the `raster` object. The resulting grids are set as attributes
        of the model with names of the form 'routing/lakesreservoirs/{grid_name}'.

        The method also retrieves the reservoir command area data from the data catalog and calculates the area of each
        command area that falls within the model region. The `waterbody_id` key is used to do the matching between these
        databases. The relative area of each command area within the model region is calculated and set as a column in
        the waterbody data. The method sets all lakes with a command area to be reservoirs and updates the waterbody data
        with any custom reservoir capacity data from the data catalog.

        TODO: Make the reservoir command area data optional.

        The resulting waterbody data is set as a table in the model with the name 'routing/lakesreservoirs/basin_lakes_data'.
        """
        self.logger.info('Setting up waterbodies')
        waterbodies = self.data_catalog.get_geodataframe(
            "hydro_lakes",
            geom=self.staticgeoms['areamaps/region'],
            predicate="intersects",
            variables=['waterbody_id', 'waterbody_type', 'volume_total', 'average_discharge', 'average_area']
        ).set_index('waterbody_id')

        self.set_grid(self.grid.raster.rasterize(
            waterbodies,
            col_name='waterbody_id',
            nodata=0,
            all_touched=True,
            dtype=np.int32
        ), name='routing/lakesreservoirs/lakesResID')
        self.subgrid.set_grid(self.subgrid.grid.raster.rasterize(
            waterbodies,
            col_name='waterbody_id',
            nodata=0,
            all_touched=True,
            dtype=np.int32
        ), name='routing/lakesreservoirs/sublakesResID')

        command_areas = self.data_catalog.get_geodataframe("reservoir_command_areas", geom=self.region, predicate="intersects")
        command_areas = command_areas[~command_areas['waterbody_id'].isnull()].reset_index(drop=True)
        command_areas['waterbody_id'] = command_areas['waterbody_id'].astype(np.int32)
        command_areas['geometry_in_region_bounds'] = gpd.overlay(command_areas, self.region, how='intersection', keep_geom_type=False)['geometry']
        command_areas['area'] = command_areas.to_crs(3857).area
        command_areas['area_in_region_bounds'] = command_areas['geometry_in_region_bounds'].to_crs(3857).area
        areas_per_waterbody = command_areas.groupby('waterbody_id').agg({'area': 'sum', 'area_in_region_bounds': 'sum'})
        relative_area_in_region = areas_per_waterbody['area_in_region_bounds'] / areas_per_waterbody['area']
        relative_area_in_region.name = 'relative_area_in_region'  # set name for merge

        self.set_grid(self.grid.raster.rasterize(
            command_areas,
            col_name='waterbody_id',
            nodata=-1,
            all_touched=True,
            dtype=np.int32
        ), name='routing/lakesreservoirs/command_areas')
        self.subgrid.set_grid(self.subgrid.grid.raster.rasterize(
            command_areas,
            col_name='waterbody_id',
            nodata=-1,
            all_touched=True,
            dtype=np.int32
        ), name='routing/lakesreservoirs/subcommand_areas')

        # set all lakes with command area to reservoir
        waterbodies['volume_flood'] = waterbodies['volume_total']
        waterbodies.loc[waterbodies.index.isin(command_areas['waterbody_id']), 'waterbody_type'] = 2
        # set relative area in region for command area. If no command area, set this is set to nan.
        waterbodies = waterbodies.merge(relative_area_in_region, how='left', left_index=True, right_index=True)

        custom_reservoir_capacity = self.data_catalog.get_dataframe("custom_reservoir_capacity").set_index('waterbody_id')
        custom_reservoir_capacity = custom_reservoir_capacity[custom_reservoir_capacity.index != -1]

        waterbodies.update(custom_reservoir_capacity)
        waterbodies = waterbodies.drop('geometry', axis=1)

        self.set_table(waterbodies, name='routing/lakesreservoirs/basin_lakes_data')

    def setup_water_demand(self):
        """
        Sets up the water demand data for GEB.

        Notes
        -----
        This method sets up the water demand data for GEB. It retrieves the domestic, industry, and
        livestock water demand data from the specified data catalog and sets it as forcing data in the model. The domestic
        water demand and consumption data are retrieved from the 'cwatm_domestic_water_demand' dataset, while the industry
        water demand and consumption data are retrieved from the 'cwatm_industry_water_demand' dataset. The livestock water
        consumption data is retrieved from the 'cwatm_livestock_water_demand' dataset.

        The domestic water demand and consumption data are provided at a monthly time step, while the industry water demand
        and consumption data are provided at an annual time step. The livestock water consumption data is provided at a
        monthly time step, but is assumed to be constant over the year.

        The resulting water demand data is set as forcing data in the model with names of the form 'water_demand/{demand_type}'.
        """
        self.logger.info('Setting up water demand')
        domestic_water_demand = self.data_catalog.get_rasterdataset('cwatm_domestic_water_demand', bbox=self.bounds, buffer=2).domWW
        domestic_water_demand['time'] = pd.date_range(start=datetime(1901, 1, 1) + relativedelta(months=int(domestic_water_demand.time[0].data.item())), periods=len(domestic_water_demand.time), freq='MS')
        domestic_water_demand.name = 'domestic_water_demand'
        self.set_forcing(domestic_water_demand.rename({'lat': 'y', 'lon': 'x'}), name='water_demand/domestic_water_demand')

        domestic_water_consumption = self.data_catalog.get_rasterdataset('cwatm_domestic_water_demand', bbox=self.bounds, buffer=2).domCon
        domestic_water_consumption.name = 'domestic_water_consumption'
        domestic_water_consumption['time'] = pd.date_range(start=datetime(1901, 1, 1) + relativedelta(months=int(domestic_water_consumption.time[0].data.item())), periods=len(domestic_water_consumption.time), freq='MS')
        self.set_forcing(domestic_water_consumption.rename({'lat': 'y', 'lon': 'x'}), name='water_demand/domestic_water_consumption')

        industry_water_demand = self.data_catalog.get_rasterdataset('cwatm_industry_water_demand', bbox=self.bounds, buffer=2).indWW
        industry_water_demand['time'] = pd.date_range(start=datetime(1901 + int(industry_water_demand.time[0].data.item()), 1, 1), periods=len(industry_water_demand.time), freq='AS')
        industry_water_demand.name = 'industry_water_demand'
        self.set_forcing(industry_water_demand.rename({'lat': 'y', 'lon': 'x'}), name='water_demand/industry_water_demand')

        industry_water_consumption = self.data_catalog.get_rasterdataset('cwatm_industry_water_demand', bbox=self.bounds, buffer=2).indCon
        industry_water_consumption.name = 'industry_water_consumption'
        industry_water_consumption['time'] = pd.date_range(start=datetime(1901 + int(industry_water_consumption.time[0].data.item()), 1, 1), periods=len(industry_water_consumption.time), freq='AS')
        self.set_forcing(industry_water_consumption.rename({'lat': 'y', 'lon': 'x'}), name='water_demand/industry_water_consumption')

        livestock_water_consumption = self.data_catalog.get_rasterdataset('cwatm_livestock_water_demand', bbox=self.bounds, buffer=2)
        livestock_water_consumption['time'] = pd.date_range(start=datetime(1901, 1, 1) + relativedelta(months=int(livestock_water_consumption.time[0].data.item())), periods=len(livestock_water_consumption.time), freq='MS')
        livestock_water_consumption.name = 'livestock_water_consumption'
        self.set_forcing(livestock_water_consumption.rename({'lat': 'y', 'lon': 'x'}), name='water_demand/livestock_water_consumption')

    def setup_modflow(self, epsg: int, resolution: float):
        """
        Sets up the MODFLOW grid for GEB.

        Parameters
        ----------
        epsg : int
            The EPSG code for the coordinate reference system of the model grid.
        resolution : float
            The resolution of the model grid in meters.

        Notes
        -----
        This method sets up the MODFLOW grid for GEB. These grids don't match because one is based on
        a geographic coordinate reference system and the other is based on a projected coordinate reference system. Therefore,
        this function creates a projected MODFLOW grid and then calculates the intersection between the model grid and the MODFLOW
        grid.

        It first retrieves the MODFLOW mask from the `get_modflow_transform_and_shape` function, which calculates the affine
        transform and shape of the MODFLOW grid based on the resolution and EPSG code of the model grid. The MODFLOW mask is
        created using the `full_from_transform` method of the `raster` object, which creates a binary grid with the same affine
        transform and shape as the MODFLOW grid.

        The method then creates an intersection between the model grid and the MODFLOW grid using the `create_indices`
        function. The resulting indices are used to match cells between the model grid and the MODFLOW grid. The indices
        are saved for use in the model.

        Finally, the elevation data for the MODFLOW grid is retrieved from the MERIT dataset and reprojected to the MODFLOW
        grid using the `reproject_like` method of the `raster` object. The resulting elevation grid is set as a grid in the
        model with the name 'groundwater/modflow/modflow_elevation'.
        """
        self.logger.info("Setting up MODFLOW")
        modflow_affine, MODFLOW_shape = get_modflow_transform_and_shape(
            self.grid.mask,
            4326,
            epsg,
            resolution
        )
        modflow_mask = hydromt.raster.full_from_transform(
            modflow_affine,
            MODFLOW_shape,
            nodata=0,
            dtype=np.int8,
            name=f'groundwater/modflow/modflow_mask',
            crs=epsg,
            lazy=True
        )

        intersection = create_indices(
            self.grid.mask.raster.transform,
            self.grid.mask.raster.shape,
            4326,
            modflow_affine,
            MODFLOW_shape,
            epsg
        )

        self.set_binary(intersection['y_modflow'], name=f'groundwater/modflow/y_modflow')
        self.set_binary(intersection['x_modflow'], name=f'groundwater/modflow/x_modflow')
        self.set_binary(intersection['y_hydro'], name=f'groundwater/modflow/y_hydro')
        self.set_binary(intersection['x_hydro'], name=f'groundwater/modflow/x_hydro')
        self.set_binary(intersection['area'], name=f'groundwater/modflow/area')

        modflow_mask.data = create_modflow_basin(self.grid.mask, intersection, MODFLOW_shape)
        self.MODFLOW_grid.set_grid(modflow_mask, name=f'groundwater/modflow/modflow_mask')

        MERIT = self.data_catalog.get_rasterdataset("merit_hydro", variables=['elv'])
        MERIT_x_step = MERIT.coords['x'][1] - MERIT.coords['x'][0]
        MERIT_y_step = MERIT.coords['y'][0] - MERIT.coords['y'][1]
        MERIT = MERIT.assign_coords(
            x=MERIT.coords['x'] + MERIT_x_step / 2,
            y=MERIT.coords['y'] + MERIT_y_step / 2
        )
        elevation_modflow = MERIT.raster.reproject_like(modflow_mask, method='average')

        self.MODFLOW_grid.set_grid(elevation_modflow, name=f'groundwater/modflow/modflow_elevation')

    def setup_forcing(
            self,
            starttime: date,
            endtime: date,
            data_source: str='isimip',
            resolution_arcsec: int=30,
            forcing: str='chelsa-w5e5v1.0',
            scenario_name: str=None,
            ssp=None
        ):
        """
        Sets up the forcing data for GEB.

        Parameters
        ----------
        starttime : date
            The start time of the forcing data.
        endtime : date
            The end time of the forcing data.
        data_source : str, optional
            The data source to use for the forcing data. Default is 'isimip'.

        Notes
        -----
        This method sets up the forcing data for GEB. It first downloads the high-resolution variables
        (precipitation, surface solar radiation, air temperature, maximum air temperature, and minimum air temperature) from
        the ISIMIP dataset for the specified time period. The data is downloaded using the `setup_30arcsec_variables_isimip`
        method.

        The method then sets up the relative humidity, longwave radiation, pressure, and wind data for the model. The
        relative humidity data is downloaded from the ISIMIP dataset using the `setup_hurs_isimip_30arcsec` method. The longwave radiation
        data is calculated using the air temperature and relative humidity data and the `calculate_longwave` function. The
        pressure data is downloaded from the ISIMIP dataset using the `setup_pressure_isimip_30arcsec` method. The wind data is downloaded
        from the ISIMIP dataset using the `setup_wind_isimip_30arcsec` method. All these data are first downscaled to the model grid.

        The resulting forcing data is set as forcing data in the model with names of the form 'forcing/{variable_name}'.
        """

        folder = 'climate'
        if scenario_name is not None:
            folder = f'{folder}/{scenario_name}'

        if data_source == 'isimip':
            if resolution_arcsec == 30:
                assert forcing == 'chelsa-w5e5v1.0', 'Only chelsa-w5e5v1.0 is supported for 30 arcsec resolution'
                # download source data from ISIMIP
                self.logger.info('setting up forcing data')
                high_res_variables = ['pr', 'rsds', 'tas', 'tasmax', 'tasmin']
                self.setup_30arcsec_variables_isimip(high_res_variables, starttime, endtime, folder=folder)
                self.logger.info('setting up relative humidity...')
                self.setup_hurs_isimip_30arcsec(starttime, endtime, folder=folder)
                self.logger.info('setting up longwave radiation...')
                self.setup_longwave_isimip_30arcsec(starttime=starttime, endtime=endtime, folder=folder)
                self.logger.info('setting up pressure...')
                self.setup_pressure_isimip_30arcsec(starttime, endtime, folder=folder)
                self.logger.info('setting up wind...')
                self.setup_wind_isimip_30arcsec(starttime, endtime, folder=folder)
            elif resolution_arcsec == 1800:
                variables = ['pr', 'rsds', 'tas', 'tasmax', 'tasmin', 'hurs', 'rlds', 'ps', 'sfcwind']
                self.setup_1800arcsec_variables_isimip(forcing, variables, starttime, endtime, ssp=ssp, folder=folder)
        elif data_source == 'cmip':
            raise NotImplementedError('CMIP forcing data is not yet supported')
        else:
            raise ValueError(f'Unknown data source: {data_source}')

        self.setup_SPEI(folder)
        self.setup_GEV(folder)

    def snap_to_grid(self, ds, reference, relative_tollerance=0.02):
        # make sure all datasets have more or less the same coordinates
        assert np.isclose(ds.coords['y'].values, reference['y'].values, atol=abs(ds.rio.resolution()[1] * relative_tollerance), rtol=0).all()
        assert np.isclose(ds.coords['x'].values, reference['x'].values, atol=abs(ds.rio.resolution()[0] * relative_tollerance), rtol=0).all()
        return ds.assign_coords(
            x=reference['x'].values,
            y=reference['y'].values,
        )

    def setup_1800arcsec_variables_isimip(self, forcing: str, variables: List[str], starttime: date, endtime: date, ssp: str, folder: str):
        """
        Sets up the high-resolution climate variables for GEB.

        Parameters
        ----------
        variables : list of str
            The list of climate variables to set up.
        starttime : date
            The start time of the forcing data.
        endtime : date
            The end time of the forcing data.
        folder: str
            The folder to save the forcing data in.

        Notes
        -----
        This method sets up the high-resolution climate variables for GEB. It downloads the specified
        climate variables from the ISIMIP dataset for the specified time period. The data is downloaded using the
        `download_isimip` method.

        The method renames the longitude and latitude dimensions of the downloaded data to 'x' and 'y', respectively. It
        then clips the data to the bounding box of the model grid using the `clip_bbox` method of the `raster` object.

        The resulting climate variables are set as forcing data in the model with names of the form '{folder}/{variable_name}'.
        """ 
        for variable in variables:
            self.logger.info(f'Setting up {variable}...')
            first_year_future_climate = 2015
            var = []
            if endtime.year < first_year_future_climate or starttime.year < first_year_future_climate:  # isimip cutoff date between historic and future climate
                ds = self.download_isimip(product='InputData', simulation_round='ISIMIP3b', climate_scenario='historical', variable=variable, starttime=starttime, endtime=endtime, forcing=forcing, resolution=None, buffer=1)
                var.append(self.interpolate(ds[variable].raster.clip_bbox(ds.raster.bounds), 'linear', xdim='lon', ydim='lat'))
            if starttime.year >= first_year_future_climate or endtime.year >= first_year_future_climate:
                assert ssp is not None, 'ssp must be specified for future climate'
                ds = self.download_isimip(product='InputData', simulation_round='ISIMIP3b', climate_scenario=ssp, variable=variable, starttime=starttime, endtime=endtime, forcing=forcing, resolution=None, buffer=1)
                var.append(self.interpolate(ds[variable].raster.clip_bbox(ds.raster.bounds), 'linear', xdim='lon', ydim='lat'))
            
            var = xr.concat(var, dim='time')
            var = var.rename({'lon': 'x', 'lat': 'y'})
            self.set_forcing(var, name=f'{folder}/{variable}')

    def setup_30arcsec_variables_isimip(self, variables: List[str], starttime: date, endtime: date, folder: str):
        """
        Sets up the high-resolution climate variables for GEB.

        Parameters
        ----------
        variables : list of str
            The list of climate variables to set up.
        starttime : date
            The start time of the forcing data.
        endtime : date
            The end time of the forcing data.
        folder: str
            The folder to save the forcing data in.

        Notes
        -----
        This method sets up the high-resolution climate variables for GEB. It downloads the specified
        climate variables from the ISIMIP dataset for the specified time period. The data is downloaded using the
        `download_isimip` method.

        The method renames the longitude and latitude dimensions of the downloaded data to 'x' and 'y', respectively. It
        then clips the data to the bounding box of the model grid using the `clip_bbox` method of the `raster` object.

        The resulting climate variables are set as forcing data in the model with names of the form 'climate/{variable_name}'.
        """ 
        for variable in variables:
            self.logger.info(f'Setting up {variable}...')
            ds = self.download_isimip(product='InputData', variable=variable, starttime=starttime, endtime=endtime, forcing='chelsa-w5e5v1.0', resolution='30arcsec')
            ds = ds.rename({'lon': 'x', 'lat': 'y'})
            var = ds[variable].raster.clip_bbox(ds.raster.bounds)
            var = self.snap_to_grid(var, self.grid.mask)
            self.set_forcing(var, name=f'{folder}/{variable}')

    def setup_hurs_isimip_30arcsec(self, starttime: date, endtime: date, folder: str):
        """
        Sets up the relative humidity data for GEB.

        Parameters
        ----------
        starttime : date
            The start time of the relative humidity data in ISO 8601 format (YYYY-MM-DD).
        endtime : date
            The end time of the relative humidity data in ISO 8601 format (YYYY-MM-DD).
        folder: str
            The folder to save the forcing data in.

        Notes
        -----
        This method sets up the relative humidity data for GEB. It first downloads the relative humidity
        data from the ISIMIP dataset for the specified time period using the `download_isimip` method. The data is downloaded
        at a 30 arcsec resolution.

        The method then downloads the monthly CHELSA-BIOCLIM+ relative humidity data at 30 arcsec resolution from the data
        catalog. The data is downloaded for each month in the specified time period and is clipped to the bounding box of
        the downloaded relative humidity data using the `clip_bbox` method of the `raster` object.

        The original ISIMIP data is then downscaled using the monthly CHELSA-BIOCLIM+ data. The downscaling method is adapted
        from https://github.com/johanna-malle/w5e5_downscale, which was licenced under GNU General Public License v3.0.

        The resulting relative humidity data is set as forcing data in the model with names of the form 'climate/hurs'.
        """
        hurs_30_min = self.download_isimip(product='SecondaryInputData', variable='hurs', starttime=starttime, endtime=endtime, forcing='w5e5v2.0', buffer=1)  # some buffer to avoid edge effects / errors in ISIMIP API

        # just taking the years to simplify things
        start_year = starttime.year
        end_year = endtime.year

        chelsa_folder = Path(self.root).parent / 'preprocessing' / 'climate' / 'chelsa-bioclim+' / 'hurs'
        chelsa_folder.mkdir(parents=True, exist_ok=True)

        self.logger.info("Downloading/reading monthly CHELSA-BIOCLIM+ hurs data at 30 arcsec resolution")
        hurs_ds_30sec, hurs_time = [], []
        for year in tqdm(range(start_year, end_year+1)):
            for month in range(1, 13):
                fn = chelsa_folder / f'hurs_{year}_{month:02d}.nc'
                if not fn.exists():
                    hurs = self.data_catalog.get_rasterdataset(f'CHELSA-BIOCLIM+_monthly_hurs_{month:02d}_{year}', bbox=hurs_30_min.raster.bounds, buffer=1)
                    del hurs.attrs['_FillValue']
                    hurs.name = 'hurs'
                    hurs.to_netcdf(fn)
                else:
                    hurs = xr.open_dataset(fn, chunks={'time': 365})['hurs']
                hurs_ds_30sec.append(hurs)
                hurs_time.append(f'{year}-{month:02d}')
        
        hurs_ds_30sec = xr.concat(hurs_ds_30sec, dim='time').rename({'x': 'lon', 'y': 'lat'})
        hurs_ds_30sec.rio.set_spatial_dims('lon', 'lat', inplace=True)
        hurs_ds_30sec['time'] = pd.date_range(hurs_time[0], hurs_time[-1], freq="MS")

        hurs_output = xr.full_like(self.forcing['climate/tas'], np.nan)
        hurs_output.name = 'hurs'
        hurs_output.attrs = {'units': '%', 'long_name': 'Relative humidity'}

        regridder = xe.Regridder(hurs_30_min.isel(time=0).drop_vars('time'), hurs_ds_30sec.isel(time=0).drop_vars('time'), "bilinear")
        for year in tqdm(range(start_year, end_year+1)):
            for month in range(1, 13):
                start_month = datetime(year, month, 1)
                end_month = datetime(year, month, monthrange(year, month)[1])
                
                w5e5_30min_sel = hurs_30_min.sel(time=slice(start_month, end_month))
                w5e5_regridded = regridder(w5e5_30min_sel) * 0.01  # convert to fraction
                w5e5_regridded_mean = w5e5_regridded.mean(dim='time')  # get monthly mean
                w5e5_regridded_tr = np.log(w5e5_regridded / (1 - w5e5_regridded))  # assume beta distribuation => logit transform
                w5e5_regridded_mean_tr = np.log(w5e5_regridded_mean / (1 - w5e5_regridded_mean))  # logit transform

                chelsa = hurs_ds_30sec.sel(time=start_month) * 0.01  # convert to fraction
                chelsa_tr = np.log(chelsa / (1 - chelsa))  # assume beta distribuation => logit transform

                difference = chelsa_tr - w5e5_regridded_mean_tr

                # apply difference to w5e5
                w5e5_regridded_tr_corr = w5e5_regridded_tr + difference
                w5e5_regridded_corr = (1 / (1 + np.exp(-w5e5_regridded_tr_corr))) * 100  # back transform
                w5e5_regridded_corr.raster.set_crs(4326)

                hurs_output.loc[
                    dict(time=slice(start_month, end_month))
                ] = w5e5_regridded_corr['hurs'].raster.clip_bbox(hurs_output.raster.bounds)

        hurs_output = self.snap_to_grid(hurs_output, self.grid.mask)
        self.set_forcing(hurs_output, f'{folder}/hurs')

    def setup_longwave_isimip_30arcsec(self, starttime: date, endtime: date, folder: str):
        """
        Sets up the longwave radiation data for GEB.

        Parameters
        ----------
        starttime : date
            The start time of the longwave radiation data in ISO 8601 format (YYYY-MM-DD).
        endtime : date
            The end time of the longwave radiation data in ISO 8601 format (YYYY-MM-DD).
        folder: str
            The folder to save the forcing data in.

        Notes
        -----
        This method sets up the longwave radiation data for GEB. It first downloads the relative humidity,
        air temperature, and downward longwave radiation data from the ISIMIP dataset for the specified time period using the
        `download_isimip` method. The data is downloaded at a 30 arcsec resolution.

        The method then regrids the downloaded data to the target grid using the `xe.Regridder` method. It calculates the
        saturation vapor pressure, water vapor pressure, clear-sky emissivity, all-sky emissivity, and cloud-based component
        of emissivity for the coarse and fine grids. It then downscales the longwave radiation data for the fine grid using
        the calculated all-sky emissivity and Stefan-Boltzmann constant. The downscaling method is adapted
        from https://github.com/johanna-malle/w5e5_downscale, which was licenced under GNU General Public License v3.0.

        The resulting longwave radiation data is set as forcing data in the model with names of the form 'climate/rlds'.
        """
        x1 = 0.43
        x2 = 5.7
        sbc = 5.67E-8   # stefan boltzman constant [Js−1 m−2 K−4]

        es0 = 6.11  # reference saturation vapour pressure  [hPa]
        T0 = 273.15
        lv = 2.5E6  # latent heat of vaporization of water
        Rv = 461.5  # gas constant for water vapour [J K kg-1]

        target = self.forcing[f'{folder}/hurs'].rename({'x': 'lon', 'y': 'lat'})

        hurs_coarse = self.download_isimip(product='SecondaryInputData', variable='hurs', starttime=starttime, endtime=endtime, forcing='w5e5v2.0', buffer=1).hurs  # some buffer to avoid edge effects / errors in ISIMIP API
        tas_coarse = self.download_isimip(product='SecondaryInputData', variable='tas', starttime=starttime, endtime=endtime, forcing='w5e5v2.0', buffer=1).tas  # some buffer to avoid edge effects / errors in ISIMIP API
        rlds_coarse = self.download_isimip(product='SecondaryInputData', variable='rlds', starttime=starttime, endtime=endtime, forcing='w5e5v2.0', buffer=1).rlds  # some buffer to avoid edge effects / errors in ISIMIP API
        
        regridder = xe.Regridder(hurs_coarse.isel(time=0).drop('time'), target, 'bilinear')

        hurs_coarse_regridded = regridder(hurs_coarse).rename({'lon': 'x', 'lat': 'y'})
        tas_coarse_regridded = regridder(tas_coarse).rename({'lon': 'x', 'lat': 'y'})
        rlds_coarse_regridded = regridder(rlds_coarse).rename({'lon': 'x', 'lat': 'y'})

        hurs_fine = self.forcing[f'{folder}/hurs']
        tas_fine = self.forcing[f'{folder}/tas']

        # now ready for calculation:
        es_coarse = es0 * np.exp((lv / Rv) * (1 / T0 - 1 / tas_coarse_regridded))  # saturation vapor pressure
        pV_coarse = (hurs_coarse_regridded * es_coarse) / 100  # water vapor pressure [hPa]

        es_fine = es0 * np.exp((lv / Rv) * (1 / T0 - 1 / tas_fine))
        pV_fine = (hurs_fine * es_fine) / 100  # water vapour pressure [hPa]

        e_cl_coarse = 0.23 + x1 * ((pV_coarse * 100) / tas_coarse_regridded) ** (1 / x2)
        # e_cl_coarse == clear-sky emissivity w5e5 (pV needs to be in Pa not hPa, hence *100)
        e_cl_fine = 0.23 + x1 * ((pV_fine * 100) / tas_fine) ** (1 / x2)
        # e_cl_fine == clear-sky emissivity target grid (pV needs to be in Pa not hPa, hence *100)

        e_as_coarse = rlds_coarse_regridded / (sbc * tas_coarse_regridded ** 4)  # all-sky emissivity w5e5
        e_as_coarse = xr.where(e_as_coarse > 1, 1, e_as_coarse)  # constrain all-sky emissivity to max 1
        delta_e = e_as_coarse - e_cl_coarse  # cloud-based component of emissivity w5e5
        
        e_as_fine = e_cl_fine + delta_e
        e_as_fine = xr.where(e_as_fine > 1, 1, e_as_fine)  # constrain all-sky emissivity to max 1
        lw_fine = e_as_fine * sbc * tas_fine ** 4  # downscaled lwr! assume cloud e is the same

        lw_fine.name = 'rlds'
        lw_fine = self.snap_to_grid(lw_fine, self.grid.mask)
        self.set_forcing(lw_fine, name=f'{folder}/rlds')

    def setup_pressure_isimip_30arcsec(self, starttime: date, endtime: date, folder: str):
        """
        Sets up the surface pressure data for GEB.

        Parameters
        ----------
        starttime : date
            The start time of the surface pressure data in ISO 8601 format (YYYY-MM-DD).
        endtime : date
            The end time of the surface pressure data in ISO 8601 format (YYYY-MM-DD).
        folder: str
            The folder to save the forcing data in.

        Notes
        -----
        This method sets up the surface pressure data for GEB. It then downloads
        the orography data and surface pressure data from the ISIMIP dataset for the specified time period using the
        `download_isimip` method. The data is downloaded at a 30 arcsec resolution.

        The method then regrids the orography and surface pressure data to the target grid using the `xe.Regridder` method.
        It corrects the surface pressure data for orography using the gravitational acceleration, molar mass of
        dry air, universal gas constant, and sea level standard temperature. The downscaling method is adapted
        from https://github.com/johanna-malle/w5e5_downscale, which was licenced under GNU General Public License v3.0.

        The resulting surface pressure data is set as forcing data in the model with names of the form 'climate/ps'.
        """
        g = 9.80665  # gravitational acceleration [m/s2]
        M = 0.02896968  # molar mass of dry air [kg/mol]
        r0 = 8.314462618  # universal gas constant [J/(mol·K)]
        T0 = 288.16  # Sea level standard temperature  [K]

        target = self.forcing[f'{folder}/hurs'].rename({'x': 'lon', 'y': 'lat'})
        pressure_30_min = self.download_isimip(product='SecondaryInputData', variable='psl', starttime=starttime, endtime=endtime, forcing='w5e5v2.0', buffer=1).psl  # some buffer to avoid edge effects / errors in ISIMIP API
        
        orography = self.download_isimip(product='InputData', variable='orog', forcing='chelsa-w5e5v1.0', buffer=1).orog  # some buffer to avoid edge effects / errors in ISIMIP API
        regridder = xe.Regridder(orography, target, 'bilinear')
        orography = regridder(orography).rename({'lon': 'x', 'lat': 'y'})

        regridder = xe.Regridder(pressure_30_min.isel(time=0).drop('time'), target, 'bilinear')
        pressure_30_min_regridded = regridder(pressure_30_min).rename({'lon': 'x', 'lat': 'y'})
        pressure_30_min_regridded_corr = pressure_30_min_regridded * np.exp(-(g * orography * M) / (T0 * r0))

        pressure = xr.full_like(self.forcing[f'{folder}/hurs'], fill_value=np.nan)
        pressure.name = 'ps'
        pressure.attrs = {'units': 'Pa', 'long_name': 'surface pressure'}
        pressure.data = pressure_30_min_regridded_corr
        
        pressure = self.snap_to_grid(pressure, self.grid.mask)
        self.set_forcing(pressure, name=f'{folder}/ps')

    def setup_wind_isimip_30arcsec(self, starttime: date, endtime: date, folder: str):
        """
        Sets up the wind data for GEB.

        Parameters
        ----------
        starttime : date
            The start time of the wind data in ISO 8601 format (YYYY-MM-DD).
        endtime : date
            The end time of the wind data in ISO 8601 format (YYYY-MM-DD).
        folder: str
            The folder to save the forcing data in.

        Notes
        -----
        This method sets up the wind data for GEB. It first downloads the global wind atlas data and
        regrids it to the target grid using the `xe.Regridder` method. It then downloads the 30-minute average wind data
        from the ISIMIP dataset for the specified time period and regrids it to the target grid using the `xe.Regridder`
        method.

        The method then creates a diff layer by assuming that wind follows a Weibull distribution and taking the log
        transform of the wind data. It then subtracts the log-transformed 30-minute average wind data from the
        log-transformed global wind atlas data to create the diff layer.

        The method then downloads the wind data from the ISIMIP dataset for the specified time period and regrids it to the
        target grid using the `xe.Regridder` method. It applies the diff layer to the log-transformed wind data and then
        exponentiates the result to obtain the corrected wind data. The downscaling method is adapted
        from https://github.com/johanna-malle/w5e5_downscale, which was licenced under GNU General Public License v3.0.

        The resulting wind data is set as forcing data in the model with names of the form 'climate/wind'.
        """
        # Can this be done with hydromt?
        global_wind_atlas = rxr.open_rasterio(self.data_catalog['global_wind_atlas'].path).rio.clip_box(*self.grid.raster.bounds)
        # TODO: Load from https in hydromt once supported: https://github.com/Deltares/hydromt/issues/499
        # global_wind_atlas = self.data_catalog.get_rasterdataset(
        #     'global_wind_atlas', bbox=self.grid.raster.bounds, buffer=10
        # ).rename({'x': 'lon', 'y': 'lat'})
        global_wind_atlas = global_wind_atlas.rename({'x': 'lon', 'y': 'lat'}).squeeze()
        target = self.grid['areamaps/grid_mask'].rename({'x': 'lon', 'y': 'lat'})
        regridder = xe.Regridder(global_wind_atlas.copy(), target, "bilinear")
        global_wind_atlas_regridded = regridder(global_wind_atlas)

        wind_30_min_avg = self.download_isimip(
            product='SecondaryInputData', 
            variable='sfcwind',
            starttime=date(2008, 1, 1),
            endtime=date(2017, 12, 31),
            forcing='w5e5v2.0',
            buffer=1
        ).sfcWind.mean(dim='time')  # some buffer to avoid edge effects / errors in ISIMIP API
        regridder_30_min = xe.Regridder(wind_30_min_avg, target, "bilinear")
        wind_30_min_avg_regridded = regridder_30_min(wind_30_min_avg)

        # create diff layer:
        # assume wind follows weibull distribution => do log transform
        wind_30_min_avg_regridded_log = np.log(wind_30_min_avg_regridded)

        global_wind_atlas_regridded_log = np.log(global_wind_atlas_regridded)

        diff_layer = global_wind_atlas_regridded_log - wind_30_min_avg_regridded_log   # to be added to log-transformed daily

        wind_30_min = self.download_isimip(product='SecondaryInputData', variable='sfcwind', starttime=starttime, endtime=endtime, forcing='w5e5v2.0', buffer=1).sfcWind  # some buffer to avoid edge effects / errors in ISIMIP API

        wind_30min_regridded = regridder_30_min(wind_30_min)
        wind_30min_regridded_log = np.log(wind_30min_regridded)

        wind_30min_regridded_log_corr = wind_30min_regridded_log + diff_layer
        wind_30min_regridded_corr = np.exp(wind_30min_regridded_log_corr)

        wind_output_clipped = wind_30min_regridded_corr.raster.clip_bbox(self.grid.raster.bounds)
        wind_output_clipped = wind_output_clipped.rename({'lon': 'x', 'lat': 'y'})
        wind_output_clipped.name = 'wind'

        wind_output_clipped = self.snap_to_grid(wind_output_clipped, self.grid.mask)
        self.set_forcing(wind_output_clipped, f'{folder}/wind')

    def setup_SPEI(self, folder):
        self.logger.info('setting up SPEI...')
        pr_data = self.forcing[f'{folder}/pr']
        tasmin_data = self.forcing[f'{folder}/tasmin']
        tasmax_data = self.forcing[f'{folder}/tasmax']

        # assert input data have the same coordinates
        assert np.array_equal(pr_data.x, tasmin_data.x)
        assert np.array_equal(pr_data.x, tasmax_data.x)
        assert np.array_equal(pr_data.y, tasmax_data.y)
        assert np.array_equal(pr_data.y, tasmax_data.y)

        # PET needs latitude, needs to be named latitude 
        tasmin_data = tasmin_data.rename({'x': 'longitude','y': 'latitude'})
        tasmax_data = tasmax_data.rename({'x': 'longitude','y': 'latitude'})

        pet = xci.potential_evapotranspiration(tasmin=tasmin_data, tasmax=tasmax_data, method='BR65')
        # Revert lon/lat to x/y
        pet = pet.rename({'longitude': 'x','latitude': 'y'})

        # Compute the potential evapotranspiration
        water_budget = xci._agro.water_budget(pr=pr_data, evspsblpot=pet)

        water_budget_positive = water_budget - 1.01*water_budget.min()
        water_budget_positive.attrs = {'units': 'kg m-2 s-1'}

        wb_cal = water_budget_positive.sel(time=slice('1981-01-01', '2010-01-01'))

        # Compute the SPEI
        spei = xci._agro.standardized_precipitation_evapotranspiration_index(wb = water_budget_positive, wb_cal = wb_cal, freq = "MS", window = 12, dist = 'gamma', method = 'APP')
        # spei = spei.compute()
        spei.attrs = {'units': '-', 'long_name': 'Standard Precipitation Evapotranspiration Index', 'name' : 'spei'}
        spei.name = 'spei'

        self.set_forcing(spei, name = f'{folder}/spei')

    def setup_GEV(self, folder):
        self.logger.info('calculating GEV parameters...')
        spei_data = self.forcing[f'{folder}/spei']

        # invert the values and take the max 
        SPEI_changed = spei_data * -1

        # Group the data by year and find the maximum monthly sum for each year
        SPEI_yearly_max = SPEI_changed.groupby('time.year').max(dim='time')

        ## Prepare the dataset for the new input values 
        NCfile_Empty = xr.Dataset(coords=spei_data.coords, attrs=spei_data.attrs)
        NCfile_Empty =  NCfile_Empty.drop_vars('time')

        attributes = ['shape', 'loc', 'scale']
        gev_datasets = {attr: NCfile_Empty.copy() for attr in attributes}

        data_shape = (NCfile_Empty.dims['y'], NCfile_Empty.dims['x'])

        for attr, dataset in gev_datasets.items():
            data = np.zeros(data_shape, dtype=np.float32)
            dataset[attr] = xr.DataArray(
                data,
                coords={
                    'y': NCfile_Empty['y'],
                    'x': NCfile_Empty['x'],
                },
                dims=('y', 'x'),
            )

        gev_shape, gev_loc, gev_scale = [gev_datasets[attr] for attr in attributes]

        # Get the latitude and longitude values
        latitude = SPEI_yearly_max.coords['y'].values
        longitude = SPEI_yearly_max.coords['x'].values

        ## Fill the new netCDF file with the GEV parameters 

        for lat_index, lat_value in enumerate(latitude):
            for lon_index, lon_value in enumerate(longitude):
                pixel_data = SPEI_yearly_max.values[:, lat_index, lon_index]
                array_spei = np.array(pixel_data)
                shape, loc, scale = genextreme.fit(array_spei)

                gev_shape['shape'][lat_index, lon_index] = np.array(shape)
                gev_loc['loc'][lat_index, lon_index] = np.array(loc)
                gev_scale['scale'][lat_index, lon_index] = np.array(scale)

        gev_shape.attrs = {'units': '-', 'long_name': 'Generalized extreme value parameters', 'name' : 'gev_shape'}
        gev_loc.attrs = {'units': '-', 'long_name': 'Generalized extreme value parameters', 'name' : 'gev_loc'}
        gev_scale.attrs = {'units': '-', 'long_name': 'Generalized extreme value parameters', 'name' : 'gev_scale'}

        self.set_grid(gev_shape['shape'], name = f'{folder}/gev_shape')
        self.set_grid(gev_loc['loc'], name = f'{folder}/gev_loc')
        self.set_grid(gev_scale['scale'], name = f'{folder}/gev_scale')

    def setup_regions_and_land_use(self, region_database='gadm_level1', unique_region_id='UID', river_threshold=100):
        """
        Sets up the (administrative) regions and land use data for GEB. The regions can be used for multiple purposes,
        for example for creating the agents in the model, assigning unique crop prices and other economic variables
        per region and for aggregating the results.

        Parameters
        ----------
        region_database : str, optional
            The name of the region database to use. Default is 'gadm_level1'.
        unique_region_id : str, optional
            The name of the column in the region database that contains the unique region ID. Default is 'UID',
            which is the unique identifier for the GADM database.
        river_threshold : int, optional
            The threshold value to use when identifying rivers in the MERIT dataset. Default is 100.

        Notes
        -----
        This method sets up the regions and land use data for GEB. It first retrieves the region data from
        the specified region database and sets it as a geometry in the model. It then pads the subgrid to cover the entire
        region and retrieves the land use data from the ESA WorldCover dataset. The land use data is reprojected to the
        padded subgrid and the region ID is rasterized onto the subgrid. The cell area for each region is calculated and
        set as a grid in the model. The MERIT dataset is used to identify rivers, which are set as a grid in the model. The
        land use data is reclassified into five classes and set as a grid in the model. Finally, the cultivated land is
        identified and set as a grid in the model.

        The resulting grids are set as attributes of the model with names of the form 'areamaps/{grid_name}' or
        'landsurface/{grid_name}'.
        """
        self.logger.info(f"Preparing regions and land use data.")
        regions = self.data_catalog.get_geodataframe(
            region_database,
            geom=self.staticgeoms['areamaps/region'],
            predicate="intersects",
        ).rename(columns={unique_region_id: 'region_id'})
        assert np.issubdtype(regions['region_id'].dtype, np.integer), "Region ID must be integer"
        assert 'ISO3' in regions.columns, f"Region database must contain ISO3 column ({self.data_catalog[region_database].path})"
        self.set_geoms(regions, name='areamaps/regions')

        region_bounds = self.geoms['areamaps/regions'].total_bounds
        
        resolution_x, resolution_y = self.subgrid.grid['areamaps/sub_grid_mask'].rio.resolution()
        pad_minx = region_bounds[0] - abs(resolution_x) / 2.0
        pad_miny = region_bounds[1] - abs(resolution_y) / 2.0
        pad_maxx = region_bounds[2] + abs(resolution_x) / 2.0
        pad_maxy = region_bounds[3] + abs(resolution_y) / 2.0

        # TODO: Is there a better way to do this?
        padded_subgrid, self.region_subgrid.slice = pad_xy(
            self.subgrid.grid['areamaps/sub_grid_mask'].rio,
            pad_minx,
            pad_miny,
            pad_maxx,
            pad_maxy,
            return_slice=True,
            constant_values=1,
        )
        padded_subgrid.raster.set_nodata(-1)
        self.region_subgrid.set_grid(padded_subgrid, name='areamaps/region_mask')
        
        land_use = self.data_catalog.get_rasterdataset(
            "esa_worldcover_2020_v100",
            geom=self.geoms['areamaps/regions'],
            buffer=200 # 2 km buffer
        )
        reprojected_land_use = land_use.raster.reproject_like(
            padded_subgrid,
            method='nearest'
        )

        region_raster = reprojected_land_use.raster.rasterize(
            self.geoms['areamaps/regions'],
            col_name='region_id',
            all_touched=True,
        )
        self.region_subgrid.set_grid(region_raster, name='areamaps/region_subgrid')

        self.grid['areamaps/cell_area']
        padded_cell_area = self.grid['areamaps/cell_area'].rio.pad_box(*region_bounds)

        # calculate the cell area for the grid for the entire region
        region_cell_area = calculate_cell_area(padded_cell_area.raster.transform, padded_cell_area.shape)

        # create subgrid for entire region
        region_cell_area_subgrid = hydromt.raster.full_from_transform(
            padded_cell_area.raster.transform * Affine.scale(1 / self.subgrid.factor),
            (padded_cell_area.raster.shape[0] * self.subgrid.factor, padded_cell_area.raster.shape[1] * self.subgrid.factor), 
            nodata=np.nan,
            dtype=padded_cell_area.dtype,
            crs=padded_cell_area.raster.crs,
            name='areamaps/sub_grid_mask',
            lazy=True
        )

        # calculate the cell area for the subgrid for the entire region
        region_cell_area_subgrid.data = repeat_grid(region_cell_area, self.subgrid.factor) / self.subgrid.factor ** 2

        # create new subgrid for the region without padding
        region_cell_area_subgrid_clipped_to_region = hydromt.raster.full(region_raster.raster.coords, nodata=np.nan, dtype=padded_cell_area.dtype, name='areamaps/sub_grid_mask', crs=region_raster.raster.crs, lazy=True)
        
        # remove padding from region subgrid
        region_cell_area_subgrid_clipped_to_region.data = region_cell_area_subgrid.raster.clip_bbox((pad_minx, pad_miny, pad_maxx, pad_maxy))

        # set the cell area for the region subgrid
        self.region_subgrid.set_grid(region_cell_area_subgrid_clipped_to_region, name='areamaps/region_cell_area_subgrid')

        MERIT = self.data_catalog.get_rasterdataset(
            "merit_hydro",
            variables=['upg'],
            bbox=padded_subgrid.rio.bounds(),
            buffer=300 # 3 km buffer
        )
        # There is a half degree offset in MERIT data
        MERIT = MERIT.assign_coords(
            x=MERIT.coords['x'] + MERIT.rio.resolution()[0] / 2,
            y=MERIT.coords['y'] - MERIT.rio.resolution()[1] / 2
        )

        # Assume all cells with at least x upstream cells are rivers.
        rivers = MERIT > river_threshold
        rivers = rivers.astype(np.int32)
        rivers.raster.set_nodata(-1)
        rivers = rivers.raster.reproject_like(reprojected_land_use, method='nearest')
        self.region_subgrid.set_grid(rivers, name='landcover/rivers')

        hydro_land_use = reprojected_land_use.raster.reclassify(
            pd.DataFrame.from_dict({
                    reprojected_land_use.raster.nodata: 5,  # no data, set to permanent water bodies because ocean
                    10: 0, # tree cover
                    20: 1, # shrubland
                    30: 1, # grassland
                    40: 1, # cropland, setting to non-irrigated. Initiated as irrigated based on agents
                    50: 4, # built-up 
                    60: 1, # bare / sparse vegetation
                    70: 1, # snow and ice
                    80: 5, # permanent water bodies
                    90: 1, # herbaceous wetland
                    95: 5, # mangroves
                    100: 1, # moss and lichen
                }, orient='index', columns=['GEB_land_use_class']
            ),
        )['GEB_land_use_class']
        hydro_land_use = xr.where(rivers != 1, hydro_land_use, 5)  # set rivers to 5 (permanent water bodies)
        hydro_land_use.raster.set_nodata(-1)
        
        self.region_subgrid.set_grid(hydro_land_use, name='landsurface/full_region_land_use_classes')

        cultivated_land = xr.where((hydro_land_use == 1) & (reprojected_land_use == 40), 1, 0)
        cultivated_land = cultivated_land.rio.set_nodata(-1)
        cultivated_land.rio.set_crs(reprojected_land_use.rio.crs)
        cultivated_land.rio.set_nodata(-1)

        self.region_subgrid.set_grid(cultivated_land, name='landsurface/full_region_cultivated_land')

        hydro_land_use_region = hydro_land_use.isel(self.region_subgrid.slice)

        # TODO: Doesn't work when using the original array. Somehow the dtype is changed on adding it to the subgrid. This is a workaround.
        self.subgrid.set_grid(hydro_land_use_region.values, name='landsurface/land_use_classes')

        cultivated_land_region = cultivated_land.isel(self.region_subgrid.slice)

        # Same workaround as above
        self.subgrid.set_grid(cultivated_land_region.values, name='landsurface/cultivated_land')

    def setup_economic_data(self):
        """
        Sets up the economic data for GEB.

        Notes
        -----
        This method sets up the lending rates and inflation rates data for GEB. It first retrieves the
        lending rates and inflation rates data from the World Bank dataset using the `get_geodataframe` method of the
        `data_catalog` object. It then creates dictionaries to store the data for each region, with the years as the time
        dimension and the lending rates or inflation rates as the data dimension.

        The lending rates and inflation rates data are converted from percentage to rate by dividing by 100 and adding 1.
        The data is then stored in the dictionaries with the region ID as the key.

        The resulting lending rates and inflation rates data are set as forcing data in the model with names of the form
        'economics/lending_rates' and 'economics/inflation_rates', respectively.
        """
        self.logger.info('Setting up economic data')
        lending_rates = self.data_catalog.get_dataframe('wb_lending_rate')
        inflation_rates = self.data_catalog.get_dataframe('wb_inflation_rate')

        lending_rates_dict, inflation_rates_dict = {'data': {}}, { 'data': {}}
        years_lending_rates = [c for c in lending_rates.columns if c.isnumeric() and len(c) == 4 and int(c) >= 1900 and int(c) <= 3000]
        lending_rates_dict['time'] = years_lending_rates
        years_inflation_rates = [c for c in inflation_rates.columns if c.isnumeric() and len(c) == 4 and int(c) >= 1900 and int(c) <= 3000]
        inflation_rates_dict['time'] = years_inflation_rates
        for _, region in self.geoms['areamaps/regions'].iterrows():
            region_id = region['region_id']
            ISO3 = region['ISO3']

            lending_rates_country = (lending_rates.loc[lending_rates["Country Code"] == ISO3, years_lending_rates] / 100 + 1)  # percentage to rate
            assert len(lending_rates_country) == 1, f"Expected one row for {ISO3}, got {len(lending_rates_country)}"
            lending_rates_dict['data'][region_id] = lending_rates_country.iloc[0].tolist()

            inflation_rates_country = (inflation_rates.loc[inflation_rates["Country Code"] == ISO3, years_inflation_rates] / 100 + 1) # percentage to rate
            assert len(inflation_rates_country) == 1, f"Expected one row for {ISO3}, got {len(inflation_rates_country)}"
            inflation_rates_dict['data'][region_id] = inflation_rates_country.iloc[0].tolist()

        self.set_dict(inflation_rates_dict, name='economics/inflation_rates')
        self.set_dict(lending_rates_dict, name='economics/lending_rates')

    def setup_well_prices_by_reference_year(self, well_price: float, upkeep_price_per_m2: float, reference_year: int, start_year: int, end_year: int):
        """
        Sets up the well prices and upkeep prices for the hydrological model based on a reference year.

        Parameters
        ----------
        well_price : float
            The price of a well in the reference year.
        upkeep_price_per_m2 : float
            The upkeep price per square meter of a well in the reference year.
        reference_year : int
            The reference year for the well prices and upkeep prices.
        start_year : int
            The start year for the well prices and upkeep prices.
        end_year : int
            The end year for the well prices and upkeep prices.

        Notes
        -----
        This method sets up the well prices and upkeep prices for the hydrological model based on a reference year. It first
        retrieves the inflation rates data from the `economics/inflation_rates` dictionary. It then creates dictionaries to
        store the well prices and upkeep prices for each region, with the years as the time dimension and the prices as the
        data dimension.

        The well prices and upkeep prices are calculated by applying the inflation rates to the reference year prices. The
        resulting prices are stored in the dictionaries with the region ID as the key.

        The resulting well prices and upkeep prices data are set as dictionary with names of the form
        'economics/well_prices' and 'economics/upkeep_prices_well_per_m2', respectively.
        """
        self.logger.info('Setting up well prices by reference year')
        # create dictory with prices for well_prices per year by applying inflation rates
        inflation_rates = self.dict['economics/inflation_rates']
        regions = list(inflation_rates['data'].keys())

        well_prices_dict = {
            'time': list(range(start_year, end_year + 1)),
            'data': {}
        }
        for region in regions:
            well_prices = pd.Series(index=range(start_year, end_year + 1))
            well_prices.loc[reference_year] = well_price
            
            for year in range(reference_year + 1, end_year + 1):
                well_prices.loc[year] = well_prices[year-1] * inflation_rates['data'][region][inflation_rates['time'].index(str(year))]
            for year in range(reference_year -1, start_year -1, -1):
                well_prices.loc[year] = well_prices[year+1] / inflation_rates['data'][region][inflation_rates['time'].index(str(year+1))]

            well_prices_dict['data'][region] = well_prices.tolist()

        self.set_dict(well_prices_dict, name='economics/well_prices')
            
        upkeep_prices_dict = {
            'time': list(range(start_year, end_year + 1)),
            'data': {}
        }
        for region in regions:
            upkeep_prices = pd.Series(index=range(start_year, end_year + 1))
            upkeep_prices.loc[reference_year] = upkeep_price_per_m2
            
            for year in range(reference_year + 1, end_year + 1):
                upkeep_prices.loc[year] = upkeep_prices[year-1] * inflation_rates['data'][region][inflation_rates['time'].index(str(year))]
            for year in range(reference_year -1, start_year -1, -1):
                upkeep_prices.loc[year] = upkeep_prices[year+1] / inflation_rates['data'][region][inflation_rates['time'].index(str(year+1))]

            upkeep_prices_dict['data'][region] = upkeep_prices.tolist()

        self.set_dict(upkeep_prices_dict, name='economics/upkeep_prices_well_per_m2')

    def setup_drip_irrigation_prices_by_reference_year(self, drip_irrigation_price: float, upkeep_price_per_m2: float, reference_year: int, start_year: int, end_year: int):
        """
        Sets up the drip_irrigation prices and upkeep prices for the hydrological model based on a reference year.

        Parameters
        ----------
        drip_irrigation_price : float
            The price of a drip_irrigation in the reference year.
        upkeep_price_per_m2 : float
            The upkeep price per square meter of a drip_irrigation in the reference year.
        reference_year : int
            The reference year for the drip_irrigation prices and upkeep prices.
        start_year : int
            The start year for the drip_irrigation prices and upkeep prices.
        end_year : int
            The end year for the drip_irrigation prices and upkeep prices.

        Notes
        -----
        This method sets up the drip_irrigation prices and upkeep prices for the hydrological model based on a reference year. It first
        retrieves the inflation rates data from the `economics/inflation_rates` dictionary. It then creates dictionaries to
        store the drip_irrigation prices and upkeep prices for each region, with the years as the time dimension and the prices as the
        data dimension.

        The drip_irrigation prices and upkeep prices are calculated by applying the inflation rates to the reference year prices. The
        resulting prices are stored in the dictionaries with the region ID as the key.

        The resulting drip_irrigation prices and upkeep prices data are set as dictionary with names of the form
        'economics/drip_irrigation_prices' and 'economics/upkeep_prices_drip_irrigation_per_m2', respectively.
        """
        self.logger.info('Setting up drip_irrigation prices by reference year')
        # create dictory with prices for drip_irrigation_prices per year by applying inflation rates
        inflation_rates = self.dict['economics/inflation_rates']
        regions = list(inflation_rates['data'].keys())

        drip_irrigation_prices_dict = {
            'time': list(range(start_year, end_year + 1)),
            'data': {}
        }
        for region in regions:
            drip_irrigation_prices = pd.Series(index=range(start_year, end_year + 1))
            drip_irrigation_prices.loc[reference_year] = drip_irrigation_price
            
            for year in range(reference_year + 1, end_year + 1):
                drip_irrigation_prices.loc[year] = drip_irrigation_prices[year-1] * inflation_rates['data'][region][inflation_rates['time'].index(str(year))]
            for year in range(reference_year -1, start_year -1, -1):
                drip_irrigation_prices.loc[year] = drip_irrigation_prices[year+1] / inflation_rates['data'][region][inflation_rates['time'].index(str(year+1))]

            drip_irrigation_prices_dict['data'][region] = drip_irrigation_prices.tolist()

        self.set_dict(drip_irrigation_prices_dict, name='economics/drip_irrigation_prices')
            
        upkeep_prices_dict = {
            'time': list(range(start_year, end_year + 1)),
            'data': {}
        }
        for region in regions:
            upkeep_prices = pd.Series(index=range(start_year, end_year + 1))
            upkeep_prices.loc[reference_year] = upkeep_price_per_m2
            
            for year in range(reference_year + 1, end_year + 1):
                upkeep_prices.loc[year] = upkeep_prices[year-1] * inflation_rates['data'][region][inflation_rates['time'].index(str(year))]
            for year in range(reference_year -1, start_year -1, -1):
                upkeep_prices.loc[year] = upkeep_prices[year+1] / inflation_rates['data'][region][inflation_rates['time'].index(str(year+1))]

            upkeep_prices_dict['data'][region] = upkeep_prices.tolist()

        self.set_dict(upkeep_prices_dict, name='economics/upkeep_prices_drip_irrigation_per_m2')

    def setup_farmers(self, farmers, irrigation_sources=None, n_seasons=1):
        """
        Sets up the farmers data for GEB.

        Parameters
        ----------
        farmers : pandas.DataFrame
            A DataFrame containing the farmer data.
        irrigation_sources : dict, optional
            A dictionary mapping irrigation source names to IDs.
        n_seasons : int, optional
            The number of seasons to simulate.

        Notes
        -----
        This method sets up the farmers data for GEB. It first retrieves the region data from the
        `areamaps/regions` and `areamaps/region_subgrid` grids. It then creates a `farms` grid with the same shape as the
        `region_subgrid` grid, with a value of -1 for each cell.

        For each region, the method clips the `cultivated_land` grid to the region and creates farms for the region using
        the `create_farms` function, using these farmlands as well as the dataframe of farmer agents. The resulting farms
        whose IDs correspondd to the IDs in the farmer dataframe are added to the `farms` grid for the region.

        The method then removes any farms that are outside the study area by using the `region_mask` grid. It then remaps
        the farmer IDs to a contiguous range of integers starting from 0.

        The resulting farms data is set as agents data in the model with names of the form 'agents/farmers/farms'. The
        crop names are mapped to IDs using the `crop_name_to_id` dictionary that was previously created. The resulting
        crop IDs are stored in the `season_#_crop` columns of the `farmers` DataFrame.

        If `irrigation_sources` is provided, the method sets the `irrigation_source` column of the `farmers` DataFrame to
        the corresponding IDs.

        Finally, the method sets the binary data for each column of the `farmers` DataFrame as agents data in the model
        with names of the form 'agents/farmers/{column}'.
        """
        regions = self.geoms['areamaps/regions']
        regions_raster = self.region_subgrid.grid['areamaps/region_subgrid']
        
        farms = hydromt.raster.full_like(regions_raster, nodata=-1, lazy=True)
        
        for region_id in regions['region_id']:
            self.logger.info(f"Creating farms for region {region_id}")
            region = regions_raster == region_id
            region_clip, bounds = clip_with_grid(region, region)

            cultivated_land_region = self.region_subgrid.grid['landsurface/full_region_cultivated_land'].isel(bounds)
            cultivated_land_region = xr.where(region_clip, cultivated_land_region, 0)
            # TODO: Why does nodata value disappear?                  
            farmers_region = farmers[farmers['region_id'] == region_id]
            farms_region = create_farms(farmers_region, cultivated_land_region, farm_size_key='area_n_cells')
            assert farms_region.min() >= -1  # -1 is nodata value, all farms should be positive

            farms[bounds] = xr.where(region_clip, farms_region, farms.isel(bounds))
        
        farmers = farmers.drop('area_n_cells', axis=1)

        region_mask = self.region_subgrid.grid['areamaps/region_mask'].astype(bool)

        # TODO: Again why is dtype changed? And export doesn't work?

        cut_farms = np.unique(xr.where(region_mask, farms.copy().values, -1))
        cut_farms = cut_farms[cut_farms != -1]

        assert farms.min() >= -1  # -1 is nodata value, all farms should be positive
        subgrid_farms = clip_with_grid(farms, ~region_mask)[0]

        subgrid_farms_in_study_area = xr.where(np.isin(subgrid_farms, cut_farms), -1, subgrid_farms)
        farmers = farmers[~farmers.index.isin(cut_farms)]

        remap_farmer_ids = np.full(farmers.index.max() + 2, -1, dtype=np.int32) # +1 because 0 is also a farm, +1 because no farm is -1, set to -1 in next step
        remap_farmer_ids[farmers.index] = np.arange(len(farmers))
        subgrid_farms_in_study_area = remap_farmer_ids[subgrid_farms_in_study_area.values]

        farmers = farmers.reset_index(drop=True)
        
        assert np.setdiff1d(np.unique(subgrid_farms_in_study_area), -1).size == len(farmers)
        assert farmers.iloc[-1].name == subgrid_farms_in_study_area.max()

        self.subgrid.set_grid(subgrid_farms_in_study_area, name='agents/farmers/farms')
        self.subgrid.grid['agents/farmers/farms'].rio.set_nodata(-1)

        crop_name_to_id = {
            crop_name: int(ID)
            for ID, crop_name in self.dict['crops/crop_ids'].items()
        }
        crop_name_to_id[np.nan] = -1
        for season in range(1, n_seasons + 1):
            farmers[f'season_#{season}_crop'] = farmers[f'season_#{season}_crop'].map(crop_name_to_id)

        if irrigation_sources:
            self.set_dict(irrigation_sources, name='agents/farmers/irrigation_sources')
            farmers['irrigation_source'] = farmers['irrigation_source'].map(irrigation_sources)

        for column in farmers.columns:
            self.set_binary(farmers[column], name=f'agents/farmers/{column}')

    def setup_farmers_from_csv(self, path=None, irrigation_sources=None, n_seasons=1):
        """
        Sets up the farmers data for GEB from a CSV file.

        Parameters
        ----------
        path : str
            The path to the CSV file containing the farmer data.
        irrigation_sources : dict, optional
            A dictionary mapping irrigation source names to IDs.
        n_seasons : int, optional
            The number of seasons to simulate.

        Notes
        -----
        This method sets up the farmers data for GEB from a CSV file. It first reads the farmer data from
        the CSV file using the `pandas.read_csv` method. The resulting DataFrame is passed to the `setup_farmers` method
        along with the optional `irrigation_sources` and `n_seasons` parameters.

        See the `setup_farmers` method for more information on how the farmer data is set up in the model.
        """
        if path is None:
            path = Path(self.root).parent / 'preprocessing' / 'agents' / 'farmers' / 'farmers.csv'
        farmers = pd.read_csv(path, index_col=0)
        self.setup_farmers(farmers, irrigation_sources, n_seasons)

    def setup_farmers_simple(
        self,
        irrigation_sources,
        region_id_column='UID',
        country_iso3_column='ISO3',
        risk_aversion_mean=1.5,
        risk_aversion_standard_deviation=0.5,
    ):
        """
        Sets up the farmers for GEB.

        Parameters
        ----------
        irrigation_sources : dict
            A dictionary of irrigation sources and their corresponding water availability in m^3/day.
        region_id_column : str, optional
            The name of the column in the region database that contains the region IDs. Default is 'UID'.
        country_iso3_column : str, optional
            The name of the column in the region database that contains the country ISO3 codes. Default is 'ISO3'.
        risk_aversion_mean : float, optional
            The mean of the normal distribution from which the risk aversion values are sampled. Default is 1.5.
        risk_aversion_standard_deviation : float, optional
            The standard deviation of the normal distribution from which the risk aversion values are sampled. Default is 0.5.

        Notes
        -----
        This method sets up the farmers for GEB. This is a simplified method that generates an example set of agent data.
        It first calculates the number of farmers and their farm sizes for each region based on the agricultural data for
        that region based on theamount of farm land and data from a global database on farm sizes per country. It then
        randomly assigns crops, irrigation sources, household sizes, and daily incomes and consumption levels to each farmer.

        A paper that reports risk aversion values for 75 countries is this one: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2646134
        """
        SIZE_CLASSES_BOUNDARIES = {
            '< 1 Ha': (0, 10000),
            '1 - 2 Ha': (10000, 20000),
            '2 - 5 Ha': (20000, 50000),
            '5 - 10 Ha': (50000, 100000),
            '10 - 20 Ha': (100000, 200000),
            '20 - 50 Ha': (200000, 500000),
            '50 - 100 Ha': (500000, 1000000),
            '100 - 200 Ha': (1000000, 2000000),
            '200 - 500 Ha': (2000000, 5000000),
            '500 - 1000 Ha': (5000000, 10000000),
            '> 1000 Ha': (10000000, np.inf)
        }

        cultivated_land = self.region_subgrid.grid['landsurface/full_region_cultivated_land']

        regions_grid = self.region_subgrid.grid['areamaps/region_subgrid']

        cell_area = self.region_subgrid.grid['areamaps/region_cell_area_subgrid']

        regions_shapes = self.geoms['areamaps/regions']
        assert country_iso3_column in regions_shapes.columns, f"Region database must contain {country_iso3_column} column ({self.data_catalog['gadm_level1'].path})"

        farm_sizes_per_country = self.data_catalog.get_dataframe('lowder_farm_sizes').dropna(subset=['Total'], axis=0).drop(['empty', 'income class'], axis=1)
        farm_sizes_per_country['Country'] = farm_sizes_per_country['Country'].ffill()
        # Remove preceding and trailing white space from country names
        farm_sizes_per_country['Country'] = farm_sizes_per_country['Country'].str.strip()
        farm_sizes_per_country['Census Year'] = farm_sizes_per_country['Country'].ffill()

        # convert country names to ISO3 codes
        iso3_codes = {
            "Albania": "ALB",
            "Algeria": "DZA",
            "American Samoa": "ASM",
            "Argentina": "ARG",
            "Austria": "AUT",
            "Bahamas": "BHS",
            "Barbados": "BRB",
            "Belgium": "BEL",
            "Brazil": "BRA",
            "Bulgaria": "BGR",
            "Burkina Faso": "BFA",
            "Chile": "CHL",
            "Colombia": "COL",
            "Côte d'Ivoire": "CIV",
            "Croatia": "HRV",
            "Cyprus": "CYP",
            "Czech Republic": "CZE",
            "Democratic Republic of the Congo": "COD",
            "Denmark": "DNK",
            "Dominica": "DMA",
            "Ecuador": "ECU",
            "Egypt": "EGY",
            "Estonia": "EST",
            "Ethiopia": "ETH",
            "Fiji": "FJI",
            "Finland": "FIN",
            "France": "FRA",
            "French Polynesia": "PYF",
            "Georgia": "GEO",
            "Germany": "DEU",
            "Greece": "GRC",
            "Grenada": "GRD",
            "Guam": "GUM",
            "Guatemala": "GTM",
            "Guinea": "GIN",
            "Honduras": "HND",
            "India": "IND",
            "Indonesia": "IDN",
            "Iran (Islamic Republic of)": "IRN",
            "Ireland": "IRL",
            "Italy": "ITA",
            "Japan": "JPN",
            "Jamaica": "JAM",
            "Jordan": "JOR",
            "Korea, Rep. of": "KOR",
            "Kyrgyzstan": "KGZ",
            "Lao People's Democratic Republic": "LAO",
            "Latvia": "LVA",
            "Lebanon": "LBN",
            "Lithuania": "LTU",
            "Luxembourg": "LUX",
            "Malta": "MLT",
            "Morocco": "MAR",
            "Myanmar": "MMR",
            "Namibia": "NAM",
            "Nepal": "NPL",
            "Netherlands": "NLD",
            "Nicaragua": "NIC",
            "Northern Mariana Islands": "MNP",
            "Norway": "NOR",
            "Pakistan": "PAK",
            "Panama": "PAN",
            "Paraguay": "PRY",
            "Peru": "PER",
            "Philippines": "PHL",
            "Poland": "POL",
            "Portugal": "PRT",
            "Puerto Rico": "PRI",
            "Qatar": "QAT",
            "Romania": "ROU",
            "Saint Lucia": "LCA",
            "Saint Vincent and the Grenadines": "VCT",
            "Samoa": "WSM",
            "Senegal": "SEN",
            "Serbia": "SRB",
            "Sweden": "SWE",
            "Switzerland": "CHE",
            "Thailand": "THA",
            "Trinidad and Tobago": "TTO",
            "Turkey": "TUR",
            "Uganda": "UGA",
            "United Kingdom": "GBR",
            "United States of America": "USA",
            "Uruguay": "URY",
            "Venezuela (Bolivarian Republic of)": "VEN",
            "Virgin Islands, United States": "VIR",
            "Yemen": "YEM",
            "Cook Islands": "COK",
            "French Guiana": "GUF",
            "Guadeloupe": "GLP",
            "Martinique": "MTQ",
            "Réunion": "REU",
            "Canada": "CAN",
            "China": "CHN",
            "Guinea Bissau": "GNB",
            "Hungary": "HUN",
            "Lesotho": "LSO",
            "Libya": "LBY",
            "Malawi": "MWI",
            "Mozambique": "MOZ",
            "New Zealand": "NZL",
            "Slovakia": "SVK",
            "Slovenia": "SVN",
            "Spain": "ESP",
            "St. Kitts & Nevis": "KNA",
            "Viet Nam": "VNM",
            "Australia": "AUS",
            "Djibouti": "DJI",
            "Mali": "MLI",
            "Togo": "TGO",
            "Zambia": "ZMB"
        }
        farm_sizes_per_country['ISO3'] = farm_sizes_per_country['Country'].map(iso3_codes)
        assert not farm_sizes_per_country['ISO3'].isna().any(), f"Found {farm_sizes_per_country['ISO3'].isna().sum()} countries without ISO3 code"

        all_agents = []
        for _, region in regions_shapes.iterrows():
            UID = region[region_id_column]
            country_ISO3 = region[country_iso3_column]
            self.logger.debug(f'Processing region {UID} in {country_ISO3}')

            cultivated_land_region = ((regions_grid == UID) & (cultivated_land == True))
            total_cultivated_land_area_lu = (((regions_grid == UID) & (cultivated_land == True)) * cell_area).sum()
            average_cell_area_region = cell_area.where(((regions_grid == UID) & (cultivated_land == True))).mean()

            country_farm_sizes = farm_sizes_per_country.loc[(farm_sizes_per_country['ISO3'] == country_ISO3)].drop(['Country', "Census Year", "Total"], axis=1)
            assert len(country_farm_sizes) == 2, f'Found {len(country_farm_sizes) / 2} country_farm_sizes for {country_ISO3}'
            
            n_holdings = country_farm_sizes.loc[
                country_farm_sizes['Holdings/ agricultural area'] == 'Holdings'
            ].iloc[0].drop(['Holdings/ agricultural area', 'ISO3']).replace('..', '0').astype(np.int64)
            agricultural_area_db_ha = country_farm_sizes.loc[
                country_farm_sizes['Holdings/ agricultural area'] == 'Agricultural area (Ha) '
            ].iloc[0].drop(['Holdings/ agricultural area', 'ISO3']).replace('..', '0').astype(np.int64)
            agricultural_area_db = agricultural_area_db_ha * 10000
            avg_size_class = agricultural_area_db / n_holdings
            
            total_cultivated_land_area_db = agricultural_area_db.sum()

            n_cells_per_size_class = pd.Series(0, index=n_holdings.index)

            for size_class in agricultural_area_db.index:
                if n_holdings[size_class] > 0:  # if no holdings, no need to calculate
                    n_holdings[size_class] = n_holdings[size_class] * (total_cultivated_land_area_lu / total_cultivated_land_area_db)
                    n_cells_per_size_class.loc[size_class] = n_holdings[size_class] * avg_size_class[size_class] / average_cell_area_region
                    assert not np.isnan(n_cells_per_size_class.loc[size_class])

            assert math.isclose(cultivated_land_region.sum(), n_cells_per_size_class.sum())
            
            whole_cells_per_size_class = (n_cells_per_size_class // 1).astype(int)
            leftover_cells_per_size_class = n_cells_per_size_class % 1
            whole_cells = whole_cells_per_size_class.sum()
            n_missing_cells = cultivated_land_region.sum() - whole_cells
            assert n_missing_cells <= len(agricultural_area_db)

            index = list(zip(leftover_cells_per_size_class.index, leftover_cells_per_size_class % 1))
            n_cells_to_add = sorted(index, key=lambda x: x[1], reverse=True)[:n_missing_cells.compute().item()]
            whole_cells_per_size_class.loc[[p[0] for p in n_cells_to_add]] += 1

            region_agents = []
            for size_class in whole_cells_per_size_class.index:
                
                # if no cells for this size class, just continue
                if whole_cells_per_size_class.loc[size_class] == 0:
                    continue
                
                min_size_m2, max_size_m2 = SIZE_CLASSES_BOUNDARIES[size_class]

                min_size_cells = int(min_size_m2 / average_cell_area_region)
                min_size_cells = max(min_size_cells, 1)  # farm can never be smaller than one cell
                max_size_cells = int(max_size_m2 / average_cell_area_region) - 1  # otherwise they overlap with next size class
                mean_cells_per_agent = int(avg_size_class[size_class] / average_cell_area_region)

                if mean_cells_per_agent < min_size_cells or mean_cells_per_agent > max_size_cells:  # there must be an error in the data, thus assume centred
                    mean_cells_per_agent = (min_size_cells + max_size_cells) // 2

                number_of_agents_size_class = round(n_holdings[size_class].compute().item())
                # if there is agricultural land, but there are no agents rounded down, we assume there is one agent
                if number_of_agents_size_class == 0 and whole_cells_per_size_class[size_class] > 0:
                    number_of_agents_size_class = 1

                population = pd.DataFrame(index=range(number_of_agents_size_class))
                
                offset = whole_cells_per_size_class[size_class] - number_of_agents_size_class * mean_cells_per_agent

                n_farms_size_class, farm_sizes_size_class = get_farm_distribution(number_of_agents_size_class, min_size_cells, max_size_cells, mean_cells_per_agent, offset)
                assert n_farms_size_class.sum() == number_of_agents_size_class
                assert (farm_sizes_size_class > 0).all()
                assert (n_farms_size_class * farm_sizes_size_class).sum() == whole_cells_per_size_class[size_class]
                farm_sizes = farm_sizes_size_class.repeat(n_farms_size_class)
                np.random.shuffle(farm_sizes)
                population['area_n_cells'] = farm_sizes
                region_agents.append(population)

                assert population['area_n_cells'].sum() == whole_cells_per_size_class[size_class]

            region_agents = pd.concat(region_agents, ignore_index=True)
            region_agents['region_id'] = UID
            all_agents.append(region_agents)

        farmers = pd.concat(all_agents, ignore_index=True)
        # randomly sample from crops        
        farmers['season_#1_crop'] = random.choices(list(self.dict['crops/crop_ids'].values()), k=len(farmers))
        farmers['season_#2_crop'] = random.choices(list(self.dict['crops/crop_ids'].values()), k=len(farmers))
        farmers['season_#3_crop'] = random.choices(list(self.dict['crops/crop_ids'].values()), k=len(farmers))
        # randomly sample from irrigation sources
        farmers['irrigation_source']= random.choices(list(irrigation_sources.keys()), k=len(farmers))

        farmers['household_size'] = random.choices([1, 2, 3, 4, 5, 6, 7], k=len(farmers))

        farmers['daily_non_farm_income_family'] = random.choices([50, 100, 200, 500], k=len(farmers))
        farmers['daily_consumption_per_capita'] = random.choices([50, 100, 200, 500], k=len(farmers))
        farmers['risk_aversion'] = np.random.normal(loc=risk_aversion_mean, scale=risk_aversion_standard_deviation, size=len(farmers))

        self.setup_farmers(farmers, irrigation_sources=irrigation_sources, n_seasons=3)

    def interpolate(self, ds, interpolation_method, ydim='y', xdim='x'):
        return ds.interp(
            method=interpolation_method,
            **{
                ydim: self.grid.coords['y'].values,
                xdim: self.grid.coords['x'].values
            }
        )

    def download_isimip(self, product, variable, forcing, starttime=None, endtime=None, simulation_round='ISIMIP3a', climate_scenario='obsclim', resolution=None, buffer=0):
        """
        Downloads ISIMIP climate data for GEB.

        Parameters
        ----------
        product : str
            The name of the ISIMIP product to download.
        variable : str
            The name of the climate variable to download.
        forcing : str
            The name of the climate forcing to download.
        starttime : date, optional
            The start date of the data. Default is None.
        endtime : date, optional
            The end date of the data. Default is None.
        resolution : str, optional
            The resolution of the data to download. Default is None.
        buffer : int, optional
            The buffer size in degrees to add to the bounding box of the data to download. Default is 0.

        Returns
        -------
        xr.Dataset
            The downloaded climate data as an xarray dataset.

        Notes
        -----
        This method downloads ISIMIP climate data for GEB. It first retrieves the dataset
        metadata from the ISIMIP repository using the specified `product`, `variable`, `forcing`, and `resolution`
        parameters. It then downloads the data files that match the specified `starttime` and `endtime` parameters, and
        extracts them to the specified `download_path` directory.

        The resulting climate data is returned as an xarray dataset. The dataset is assigned the coordinate reference system
        EPSG:4326, and the spatial dimensions are set to 'lon' and 'lat'.
        """
        # if starttime is specified, endtime must be specified as well
        assert (starttime is None) == (endtime is None)
        
        client = ISIMIPClient()
        download_path = Path(self.root).parent / 'preprocessing' / 'climate' / forcing / variable
        download_path.mkdir(parents=True, exist_ok=True)
        
        ## Code to get data from disk rather than server.
        # parse_files = []
        # for file in os.listdir(download_path):
        #     if file.endswith('.nc'):
        #         fp = download_path / file
        #         parse_files.append(fp)

        # get the dataset metadata from the ISIMIP repository
        response = client.datasets(
            simulation_round=simulation_round,
            product=product,
            climate_forcing=forcing,
            climate_scenario=climate_scenario,
            climate_variable=variable,
            resolution=resolution,
        )
        assert len(response["results"]) == 1
        dataset = response["results"][0]
        files = dataset['files']

        xmin, ymin, xmax, ymax = self.bounds
        xmin -= buffer
        ymin -= buffer
        xmax += buffer
        ymax += buffer

        # xmin = round(xmin)
        # ymin = round(ymin)
        # xmax = round(xmax)
        # ymax = round(ymax)

        if variable == 'orog':
            assert len(files) == 1
            filename = files[0]['name'] # global should be included due to error in ISIMIP API .replace('_global', '') 
            parse_files = [filename]
            if not (download_path / filename).exists():
                download_files = [files[0]['path']]
            else:
                download_files = []
                
        else:
            assert starttime is not None and endtime is not None
            download_files = []
            parse_files = []
            for file in files:
                name = file['name']
                assert name.endswith('.nc')
                splitted_filename = name.split('_')
                date = splitted_filename[-1].split('.')[0]
                if '-' in date:
                    start_date, end_date = date.split('-')
                    start_date = datetime.strptime(start_date, '%Y%m%d').date()
                    end_date = datetime.strptime(end_date, '%Y%m%d').date()
                elif len(date) == 6:
                    start_date = datetime.strptime(date, '%Y%m').date()
                    end_date = start_date + relativedelta(months=1) - relativedelta(days=1)
                elif len(date) == 4:  # is year
                    assert splitted_filename[-2].isdigit()
                    start_date = datetime.strptime(splitted_filename[-2], '%Y').date()
                    end_date = datetime.strptime(date, '%Y').date()
                else:
                    raise ValueError(f'could not parse date {date} from file {name}')

                if not (end_date < starttime or start_date > endtime):
                    parse_files.append(file['name'].replace('_global', ''))
                    if not (download_path / file['name'].replace('_global', '')).exists():
                        download_files.append(file['path'])

        if download_files:
            self.logger.info(f"Requesting download of {len(download_files)} files")
            while True:
                try:
                    response = client.cutout(download_files, [ymin, ymax, xmin, xmax])
                except requests.exceptions.HTTPError:
                    self.logger.warning("HTTPError, could not download files, retrying in 60 seconds")
                else:
                    if response['status'] == 'finished':
                        break
                    elif response['status'] == 'started':
                        self.logger.debug(f"{response['meta']['created_files']}/{response['meta']['total_files']} files prepared on ISIMIP server, waiting 60 seconds before retrying")
                    elif response['status'] == 'queued':
                        self.logger.debug("Data preparation queued on ISIMIP server, waiting 60 seconds before retrying")
                    elif response['status'] == 'failed':
                        self.logger.debug("ISIMIP internal server error, waiting 60 seconds before retrying")
                    else:
                        raise ValueError(f"Could not download files: {response['status']}")
                time.sleep(60)
            self.logger.info("Starting download of files")
            # download the file when it is ready
            client.download(
                response['file_url'],
                path=download_path,
                validate=False,
                extract=False
            )
            self.logger.info("Download finished")
            # remove zip file
            zip_file = (download_path / Path(urlparse(response['file_url']).path.split('/')[-1]))
            # make sure the file exists
            assert zip_file.exists()
            # Open the zip file
            with zipfile.ZipFile(zip_file, 'r') as zip_ref:
                # Get a list of all the files in the zip file
                file_list = [f for f in zip_ref.namelist() if f.endswith('.nc')]
                # Extract each file one by one
                for i, file_name in enumerate(file_list):
                    # Rename the file
                    bounds_str = ''
                    if isinstance(ymin, float):
                        bounds_str += f'_lat{ymin}'
                    else:
                        bounds_str += f'_lat{ymin:.1f}'
                    if isinstance(ymax, float):
                        bounds_str += f'to{ymax}'
                    else:
                        bounds_str += f'to{ymax:.1f}'
                    if isinstance(xmin, float):
                        bounds_str += f'lon{xmin}'
                    else:
                        bounds_str += f'lon{xmin:.1f}'
                    if isinstance(xmax, float):
                        bounds_str += f'to{xmax}'
                    else:
                        bounds_str += f'to{xmax:.1f}'
                    assert bounds_str in file_name
                    new_file_name = file_name.replace(bounds_str, '')
                    zip_ref.getinfo(file_name).filename = new_file_name
                    # Extract the file
                    if os.name == 'nt':
                        max_file_path_length = 260
                    else:
                        max_file_path_length = os.pathconf('/', 'PC_PATH_MAX')
                    assert len(str(download_path / new_file_name)) <= max_file_path_length, f"File path too long: {download_path / zip_ref.getinfo(file_name).filename}"
                    zip_ref.extract(file_name, path=download_path)
            # remove zip file
            (download_path / Path(urlparse(response['file_url']).path.split('/')[-1])).unlink()
            
        datasets = [
            xr.open_dataset(download_path / file, chunks={'time': 365})#.rename({'lat': 'y', 'lon': 'x'})
            for file in parse_files
        ]

        # make sure y is decreasing rather than increasing
        datasets = [
            dataset.reindex(lat = dataset.lat[::-1]) if dataset.lat[0] < dataset.lat[-1] else dataset
            for dataset in datasets   
        ]
        
        reference = datasets[0]
        for dataset in datasets:
            # make sure all datasets have more or less the same coordinates
            assert np.isclose(dataset.coords['lat'].values, reference['lat'].values, atol=abs(datasets[0].rio.resolution()[1] / 50), rtol=0).all()
            assert np.isclose(dataset.coords['lon'].values, reference['lon'].values, atol=abs(datasets[0].rio.resolution()[0] / 50), rtol=0).all()

        datasets = [
            ds.assign_coords(
                lon=reference['lon'].values,
                lat=reference['lat'].values,
                inplace=True
            ) for ds in datasets
        ]
        if len(datasets) > 1:
            ds = xr.concat(datasets, dim='time')
        else:
            ds = datasets[0]
        
        if starttime is not None:
            ds = ds.sel(time=slice(starttime, endtime))
            # assert that time is monotonically increasing with a constant step size
            assert (ds.time.diff('time').astype(np.int64) == (ds.time[1] - ds.time[0]).astype(np.int64)).all()

        ds.rio.set_spatial_dims(x_dim='lon', y_dim='lat', inplace=True)
        ds = ds.rio.write_crs(4326).rio.write_coordinate_system()
        return ds

    def add_grid_to_model_structure(self, grid: xr.Dataset, name: str) -> None:
        if name not in self.model_structure:
            self.model_structure[name] = {}
        for var_name in grid.data_vars:
            self.model_structure[name][var_name] = var_name + '.tif'
        
    def write_grid(
        self,
        driver="GTiff",
        compress="deflate",
        **kwargs,
    ) -> None:
        self._assert_write_mode
        self.grid.raster.to_mapstack(self.root, driver=driver, compress=compress, **kwargs)
        self.add_grid_to_model_structure(self.grid, 'grid')
        if len(self.subgrid._grid) > 0:
            self.subgrid.grid.raster.to_mapstack(self.root, driver=driver, compress=compress, **kwargs)
            self.add_grid_to_model_structure(self.subgrid.grid, 'subgrid')
        if len(self.region_subgrid._grid) > 0:
            self.region_subgrid.grid.raster.to_mapstack(self.root, driver=driver, compress=compress, **kwargs)
            self.add_grid_to_model_structure(self.region_subgrid.grid, 'region_subgrid')
        if len(self.MERIT_grid._grid) > 0:
            self.MERIT_grid.grid.raster.to_mapstack(self.root, driver=driver, compress=compress, **kwargs)
            self.add_grid_to_model_structure(self.MERIT_grid.grid, 'MERIT_grid')
        if len(self.MODFLOW_grid._grid) > 0:
            self.MODFLOW_grid.grid.raster.to_mapstack(self.root, driver=driver, compress=compress, **kwargs)
            self.add_grid_to_model_structure(self.MODFLOW_grid.grid, 'MODFLOW_grid')

    def write_forcing(self) -> None:
        self._assert_write_mode
        self.logger.info("Write forcing files")
        if 'forcing' not in self.model_structure:
            self.model_structure['forcing'] = {}
        for var in self.forcing:
            self.logger.info(f"Write {var}")
            forcing = self.forcing[var]
            fn = var + '.nc'
            self.model_structure['forcing'][var] = fn
            fp = Path(self.root, fn)
            fp.parent.mkdir(parents=True, exist_ok=True)
            forcing = forcing.rio.write_crs(self.crs).rio.write_coordinate_system()
            with ProgressBar():
                forcing.to_netcdf(fp, mode='w', engine="netcdf4", encoding={forcing.name: {'chunksizes': (1, forcing.y.size, forcing.x.size), "zlib": True, "complevel": 9}})

    def write_table(self):
        if len(self.table) == 0:
            self.logger.debug("No table data found, skip writing.")
        else:
            self._assert_write_mode
            if 'table' not in self.model_structure:
                self.model_structure['table'] = {}
            for name, data in self.table.items():
                fn = os.path.join(name + '.csv')
                self.model_structure['table'][name] = fn
                self.logger.debug(f"Writing file {fn}")
                data.to_csv(os.path.join(self.root, fn))

    def write_binary(self):
        if len(self.binary) == 0:
            self.logger.debug("No table data found, skip writing.")
        else:
            self._assert_write_mode
            if 'binary' not in self.model_structure:
                self.model_structure['binary'] = {}
            for name, data in self.binary.items():
                fn = os.path.join(name + '.npz')
                self.model_structure['binary'][name] = fn
                self.logger.debug(f"Writing file {fn}")
                np.savez_compressed(os.path.join(self.root, fn), data=data)

    def write_dict(self):
        if len(self.dict) == 0:
            self.logger.debug("No table data found, skip writing.")
        else:
            self._assert_write_mode
            if 'dict' not in self.model_structure:
                self.model_structure['dict'] = {}
            for name, data in self.dict.items():
                fn = os.path.join(name + '.json')
                self.model_structure['dict'][name] = fn
                self.logger.debug(f"Writing file {fn}")
                output_path = Path(self.root) / fn
                output_path.parent.mkdir(parents=True, exist_ok=True)
                with open(output_path, 'w') as f:
                    json.dump(data, f)

    def write_geoms(self, fn: str = "{name}.geojson", **kwargs) -> None:
        """Write model geometries to a vector file (by default GeoJSON) at <root>/<fn>

        key-word arguments are passed to :py:meth:`geopandas.GeoDataFrame.to_file`

        Parameters
        ----------
        fn : str, optional
            filename relative to model root and should contain a {name} placeholder,
            by default 'geoms/{name}.geojson'
        """
        if len(self._geoms) == 0:
            self.logger.debug("No geoms data found, skip writing.")
            return
        else:
            self._assert_write_mode
            if "geoms" not in self.model_structure:
                self.model_structure["geoms"] = {}
            if "driver" not in kwargs:
                kwargs.update(driver="GeoJSON")
            for name, gdf in self._geoms.items():
                self.logger.debug(f"Writing file {fn.format(name=name)}")
                self.model_structure["geoms"][name] = fn.format(name=name)
                _fn = os.path.join(self.root, fn.format(name=name))
                if not os.path.isdir(os.path.dirname(_fn)):
                    os.makedirs(os.path.dirname(_fn))
                gdf.to_file(_fn, **kwargs)

    def set_table(self, table, name):
        self.table[name] = table

    def set_binary(self, data, name):
        self.binary[name] = data

    def set_dict(self, data, name):
        self.dict[name] = data

    def write_model_structure(self):
        with open(Path(self.root, "model_structure.json"), "w") as f:
            json.dump(self.model_structure, f, indent=4)

    def write(self):
        self.write_geoms()
        self.write_forcing()
        self.write_grid()
        self.write_table()
        self.write_binary()
        self.write_dict()

        self.write_model_structure()

    def read_model_structure(self):
        if len(self.model_structure) == 0:
            with open(Path(self.root, "model_structure.json"), "r") as f:
                self.model_structure = json.load(f)

    def read_geoms(self):
        self.read_model_structure()
        for name, fn in self.model_structure["geoms"].items():
            self._geoms[name] = gpd.read_file(Path(self.root, fn))

    def read_binary(self):
        self.read_model_structure()
        for name, fn in self.model_structure["binary"].items():
            self.binary[name] = np.load(Path(self.root, fn))["data"]
    
    def read_table(self):
        self.read_model_structure()
        for name, fn in self.model_structure["table"].items():
            self.table[name] = pd.read_csv(Path(self.root, fn))

    def read_dict(self):
        self.read_model_structure()
        for name, fn in self.model_structure["dict"].items():
            with open(Path(self.root, fn), "r") as f:
                self.dict[name] = json.load(f)

    def read_grid_from_disk(self, grid, name: str) -> None:
        self.read_model_structure()
        data_arrays = []
        for name, fn in self.model_structure[name].items():
            with xr.load_dataset(Path(self.root) / fn, decode_cf=False).rename({'band_data': name}) as da:
                data_arrays.append(da.load().squeeze())
            # with xr.load_dataarray(Path(self.root) / fn, decode_cf=False) as da:
            #     data_arrays.append(da.rename(name))
        ds = xr.merge(data_arrays)
        grid.set_grid(ds)

    def read_grid(self) -> None:
        self.read_grid_from_disk(self, 'grid')
        self.read_grid_from_disk(self.subgrid, 'subgrid')
        self.read_grid_from_disk(self.region_subgrid, 'region_subgrid')
        self.read_grid_from_disk(self.MERIT_grid, 'MERIT_grid')
        self.read_grid_from_disk(self.MODFLOW_grid, 'MODFLOW_grid')

    def read_forcing(self) -> None:
        self.read_model_structure()
        for name, fn in self.model_structure['forcing'].items():
            with xr.open_dataset(Path(self.root) / fn, chunks={'time': 365})[name.split('/')[-1]] as da:
                self.set_forcing(da.load(), name=name)

    def read(self):
        self.read_model_structure()
        
        self.read_geoms()
        self.read_binary()
        self.read_table()
        self.read_dict()
        self.read_grid()
        # self.read_forcing()