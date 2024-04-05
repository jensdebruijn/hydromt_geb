import numpy as np
from datetime import date
import calendar


def get_day_of_year(date):
    return date.timetuple().tm_yday


def get_growing_season_length(start_day, end_day):
    length = (end_day - start_day) % 365
    if length == 0:
        return 365
    else:
        return length


def parse_MIRCA2000_crop_calendar(data_catalog, bounds):
    MIRCA2000_unit_grid = data_catalog.get_rasterdataset(
        "MIRCA2000_unit_grid", bbox=bounds
    )
    rainfed_crop_calendar_fp = data_catalog.get_source(
        "MIRCA2000_cropping_calendar_rainfed"
    ).path

    unique_MIRCA2000_units = np.unique(MIRCA2000_unit_grid.values)

    MIRCA2000_data = {}
    with open(rainfed_crop_calendar_fp, "r") as f:
        lines = f.readlines()
        # remove all empty lines
        lines = [line.strip() for line in lines if line.strip()]
        # skip header
        lines = lines[4:]
        for line in lines:
            line = line.replace("  ", " ").split(" ")
            unit_code = int(line[0])
            if unit_code not in unique_MIRCA2000_units:
                continue
            if unit_code not in MIRCA2000_data:
                MIRCA2000_data[unit_code] = []
            crop_class = int(line[1]) - 1  # minus one to make it zero based
            number_of_rotations = int(line[2])
            if number_of_rotations == 0:
                continue
            crops = line[3:]
            crop_rotations = []
            for rotation in range(number_of_rotations):
                area = float(crops[rotation * 3])
                if area == 0:
                    continue
                start_month = int(crops[rotation * 3 + 1])
                end_month = int(crops[rotation * 3 + 2])
                start_day = get_day_of_year(date(2000, start_month, 1))
                end_day = get_day_of_year(
                    date(2000, end_month, calendar.monthrange(2000, end_month)[1])
                )
                growth_length = get_growing_season_length(start_day, end_day)
                crop_rotations.append((start_day, growth_length, area))

            del start_month
            del end_month
            del start_day
            del end_day
            del growth_length

            # discard crop rotations with zero area
            crop_rotations = [
                crop_rotation
                for crop_rotation in crop_rotations
                if crop_rotation[2] > 0
            ]

            crop_rotations = sorted(crop_rotations, key=lambda x: x[2])  # sort by area
            if len(crop_rotations) == 1:
                start_day, growth_length, area = crop_rotations[0]
                crop_rotation = (
                    area,
                    np.array(
                        (
                            (crop_class, start_day, growth_length),
                            (-1, -1, -1),
                            (-1, -1, -1),
                        )
                    ),
                )  # -1 means no crop
                MIRCA2000_data[unit_code].append(crop_rotation)
            elif len(crop_rotations) == 2:
                # if crop rotations start on the same day, they cannot be implemented
                # by the same farmer, so we split them
                # TODO: Ensure that this only happens when the crop rotations cannot overlap.
                if crop_rotations[0][0] == crop_rotations[1][0]:
                    for crop_rotation in crop_rotations:
                        start_day, growth_length, area = crop_rotation
                        crop_rotation = (
                            area,
                            np.array(
                                (
                                    (crop_class, start_day, growth_length),
                                    (-1, -1, -1),
                                    (-1, -1, -1),
                                )
                            ),
                        )
                        MIRCA2000_data[unit_code].append(crop_rotation)
                # if the crop rotations are consecutive, we assume multi-cropping.
                else:
                    crop_rotation = (
                        crop_rotations[1][2] - crop_rotations[0][2],
                        np.array(
                            (
                                (
                                    crop_class,
                                    crop_rotations[1][0],
                                    crop_rotations[1][1],
                                ),
                                (-1, -1, -1),
                                (-1, -1, -1),
                            )
                        ),  # -1 means no crop
                    )
                    MIRCA2000_data[unit_code].append(crop_rotation)
                    crop_rotation = (
                        crop_rotations[0][2],
                        np.array(
                            (
                                (
                                    crop_class,
                                    crop_rotations[0][0],
                                    crop_rotations[0][1],
                                ),
                                (
                                    crop_class,
                                    crop_rotations[1][0],
                                    crop_rotations[1][1],
                                ),
                                (-1, -1, -1),
                            )
                        ),
                    )
                MIRCA2000_data[unit_code].append(crop_rotation)
            else:
                raise NotImplementedError

    return MIRCA2000_data
