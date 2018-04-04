"""rio_cogeo.cogeo: translate a file to a cloud optimized geotiff."""

import sys

import click

import numpy

import rasterio
from rasterio.io import MemoryFile
from rasterio.enums import Resampling
from rasterio.shutil import copy


def cog_translate(src, dst, dst_opts,
                  indexes=None, nodata=None, alpha=None, overview_level=6, config=None):
    """
    Create Cloud Optimized Geotiff.

    Parameters
    ----------
    src : str
        input dataset path.
    dst : str
        output dataset path.
    dst_opts: dict
        cloudoptimized geotiff raster profile.
    indexes : tuple, int, optional
        Raster band indexes to copy.
    nodata, int, optional
        nodata value for mask creation.
    alpha, int, optional
        alpha band index for mask creation.
    overview_level : int, optional (default: 6)
        COGEO overview (decimation) level

    """
    config = config or {}

    with rasterio.Env(**config):
        with rasterio.open(src) as src:

            indexes = indexes if indexes else src.indexes
            meta = src.meta
            meta.pop('nodata', None)
            meta.pop('alpha', None)
            meta['count'] = len(indexes)
            meta.pop('compress', None)
            meta.update(**dst_opts)

            with MemoryFile() as memfile:
                with memfile.open(**meta) as mem:
                    mask = numpy.zeros((mem.height, mem.width), dtype=numpy.uint8)
                    wind = list(mem.block_windows(1))

                    with click.progressbar(wind, length=len(wind), file=sys.stderr, show_percent=True) as windows:
                        for ij, w in windows:
                            matrix = src.read(window=w, indexes=indexes, boundless=True)
                            mem.write(matrix, window=w)

                            if nodata is not None:
                                mask_value = numpy.all(matrix != nodata, axis=0).astype(numpy.uint8) * 255
                            elif alpha is not None:
                                mask_value = src.read(alpha, window=w, boundless=True)
                            else:
                                mask_value = src.dataset_mask(window=w, boundless=True)

                            mask[w.row_off:w.row_off + w.height, w.col_off:w.col_off + w.width] = mask_value

                    mem.write_mask(mask)

                    overviews = [2**j for j in range(1, overview_level + 1)]
                    mem.build_overviews(overviews, Resampling.nearest)
                    mem.update_tags(ns='rio_overview', resampling=Resampling.nearest.value)

                    copy(mem, dst, copy_src_overviews=True, **dst_opts)


def cog_validate(src):
    """
    Create Cloud Optimized Geotiff.

    Parameters
    ----------
    src : str
        input dataset path.

    """
    errors = []
    details = {}
    with rasterio.open(src) as ds:
        if not ds.driver == 'GTiff':
            raise Exception('The file is not a GeoTIFF')

        # TODO: Overviews must be internal
        # GDAL: ds.GetFileList()

        if ds.width >= 512 or ds.height >= 512:
            if not ds.is_tiled:
                errors.append(
                    'The file is greater than 512xH or 512xW, but is not tiled')

            overviews = ds.overviews(1)
            if not overviews:
                errors.append(
                    'The file is greater than 512xH or 512xW, but has no overviews')

        ifd_offset = int(ds.get_metadata_item(1, 'IFD_OFFSET', 'TIFF'))
        ifd_offsets = [ifd_offset]
        if ifd_offset not in (8, 16):
            errors.append(
                'The offset of the main IFD should be 8 for ClassicTIFF '
                'or 16 for BigTIFF. It is %d instead'.format(ifd_offset))

        details['ifd_offsets'] = {}
        details['ifd_offsets']['main'] = ifd_offset

        if not overviews == sorted(overviews):
            errors.append('Overviews should be sorted')

        for ix, dec in enumerate(overviews):
            # TODO:
            # Check that overviews are by descending sizes
            # ovr_band = ds.GetRasterBand(1).GetOverview(i)
            # if i == 0:
            #     if (ovr_band.XSize > main_band.XSize or
            #         ovr_band.YSize > main_band.YSize):
            #             errors += [
            #                 'First overview has larger dimension than main band']
            # else:
            #     prev_ovr_band = ds.GetRasterBand(1).GetOverview(i-1)
            #     if (ovr_band.XSize > prev_ovr_band.XSize or
            #         ovr_band.YSize > prev_ovr_band.YSize):
            #             errors += [
            #                 'Overview of index %d has larger dimension than '
            #                 'overview of index %d' % (i, i-1)]
            #
            # if check_tiled:
            #     block_size = ovr_band.GetBlockSize()
            #     if block_size[0] == ovr_band.XSize and block_size[0] > 1024:
            #         errors += [
            #             'Overview of index %d is not tiled' % i]

            # Check that the IFD of descending overviews are sorted by increasing
            # offsets
            ifd_offset = int(ds.get_metadata_item(1, 'IFD_OFFSET', 'TIFF', ovr=ix))
            ifd_offsets.append(ifd_offset)

            details['ifd_offsets']['overview_{}'.format(ix)] = ifd_offset
            if ifd_offsets[-1] < ifd_offsets[-2]:
                if ix == 0:
                    errors.append(
                        'The offset of the IFD for overview of index {} is {}, '
                        'whereas it should be greater than the one of the main '
                        'image, which is at byte {}'.format(ix, ifd_offsets[-1], ifd_offsets[-2]))
                else:
                    errors.append(
                        'The offset of the IFD for overview of index {} is {}, '
                        'whereas it should be greater than the one of index {}, '
                        'which is at byte {}'.format(ix, ifd_offsets[-1], ix-1, ifd_offsets[-2]))

        block_offset = int(ds.get_metadata_item(1, 'BLOCK_OFFSET_0_0', 'TIFF'))
        if not block_offset:
            errors.append('Missing BLOCK_OFFSET_0_0')

        data_offset = int(block_offset) if block_offset else None
        data_offsets = [data_offset]
        details['data_offsets'] = {}
        details['data_offsets']['main'] = data_offset

        for ix, dec in enumerate(overviews):
            data_offset = int(ds.get_metadata_item(1, 'BLOCK_OFFSET_0_0', 'TIFF', ovr=ix))
            data_offsets.append(data_offset)
            details['data_offsets']['overview_{}'.format(ix)] = data_offset

        if data_offsets[-1] < ifd_offsets[-1]:
            if len(overviews) > 0:
                errors.append(
                    'The offset of the first block of the smallest overview '
                    'should be after its IFD')
            else:
                errors.append(
                    'The offset of the first block of the image should '
                    'be after its IFD')

        for i in range(len(data_offsets) - 2, 0, -1):
            if data_offsets[i] < data_offsets[i + 1]:
                errors.append(
                    'The offset of the first block of overview of index {} should '
                    'be after the one of the overview of index {}'.format(i - 1, i))

        if len(data_offsets) >= 2 and data_offsets[0] < data_offsets[1]:
            errors.append(
                'The offset of the first block of the main resolution image'
                'should be after the one of the overview of index {}'.format(len(overviews) - 1))

    if errors:
        for e in errors:
            click.echo(e, err=True)

        return False

    return True
