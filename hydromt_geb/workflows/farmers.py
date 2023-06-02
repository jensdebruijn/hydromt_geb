import numpy as np
import pandas as pd
from random import random
from numba import njit

@njit(cache=True)
def create_farms_numba(cultivated_land, ids, farm_sizes):
    """
    Creates random farms considering the farm size distribution.

    Args:
        cultivated_land: map of cultivated land.
        gt: geotransformation of cultivated land map.
        farm_size_probabilities: map of the probabilities for the various farm sizes to exist in a specific cell.
        farm_size_choices: Lower and upper bound of the farm size correlating to the farm size probabilities. First dimension must be equal to number of layers of farm_size_probabilities. Size of the second dimension is 2, to represent the lower and upper bound.
        cell_area: map of cell areas for all cells.

    Returns:
        farms: map of farms. Each unique ID is land owned by a single farmer. Non-cultivated land is represented by -1.
        farmer_coords: 2 dimensional numpy array of farmer locations. First dimension corresponds to the IDs of `farms`, and the second dimension are longitude and latitude.
    """

    current_farm_counter = 0
    cur_farm_size = 0
    farm_done = False

    farm_id = ids[current_farm_counter]
    farm_size = farm_sizes[current_farm_counter]
    farms = np.where(cultivated_land == True, -1, -2).astype(np.int32)
    ysize, xsize = farms.shape
    for y in range(farms.shape[0]):
        for x in range(farms.shape[1]):
            f = farms[y, x]
            if f == -1:
                assert farm_size > 0
                                
                xmin, xmax, ymin, ymax = 1e6, -1e6, 1e6, -1e6
                xlow, xhigh, ylow, yhigh = x, x+1, y, y+1

                xsearch, ysearch = 0, 0
                
                while True:
                    if not np.count_nonzero(farms[ylow:yhigh+1+ysearch, xlow:xhigh+1+xsearch] == -1):
                        break

                    for yf in range(ylow, yhigh+1):
                        for xf in range(xlow, xhigh+1):
                            if xf < xsize and yf < ysize and farms[yf, xf] == -1:
                                if xf > xmax:
                                    xmax = xf
                                if xf < xmin:
                                    xmin = xf
                                if yf > ymax:
                                    ymax = yf
                                if yf < ymin:
                                    ymin = yf
                                farms[yf, xf] = farm_id
                                cur_farm_size += 1
                                if cur_farm_size == farm_size:
                                    cur_farm_size = 0
                                    farm_done = True
                                    break
                        
                        if farm_done is True:
                            break

                    if farm_done is True:
                        break

                    if random() < 0.5:
                        ylow -=1
                        ysearch = 1
                    else:
                        yhigh +=1
                        ysearch = 0

                    if random() < 0.5:
                        xlow -= 1
                        xsearch = 1
                    else:
                        xhigh += 1
                        xsearch = 0

                if farm_done:
                    farm_done = False
                    current_farm_counter += 1
                    farm_id = ids[current_farm_counter]
                    farm_size = farm_sizes[current_farm_counter]

    assert np.count_nonzero(farms == -1) == 0
    farms = np.where(farms != -2, farms, -1)
    return farms

def create_farms(agents: pd.DataFrame, cultivated_land_tehsil: np.ndarray, farm_size_key='farm_size_n_cells') -> np.ndarray:
    assert cultivated_land_tehsil.sum() == agents[farm_size_key].sum()

    agents = agents.sample(frac=1)
    farms = create_farms_numba(
        cultivated_land_tehsil.values,
        ids=agents.index.to_numpy(),
        farm_sizes=agents[farm_size_key].to_numpy(),
    ).astype(np.int32)
    unique_farms = np.unique(farms)
    unique_farms = unique_farms[unique_farms != -1]
    if unique_farms.size > 0:
        assert unique_farms.size == unique_farms.max() + 1
    assert agents[farm_size_key].sum() == np.count_nonzero(farms != -1)
    assert farms.max() + 1 == len(agents)
    assert ((farms >= 0) == (cultivated_land_tehsil == 1)).all()
    
    return farms