from __future__ import print_function
from future_builtins import zip
from functools import partial
import os
import sys
from glob import glob
import errno
import pyproj
import numpy as np
import numpy.polynomial.polynomial as poly
import gdal
import ogr
import fiona
import fiona.crs
import matplotlib.pylab as plt
import pandas as pd
import scipy.optimize as optimize
from skimage.morphology import disk
from scipy.ndimage.morphology import binary_opening
from matplotlib.backends.backend_pdf import PdfPages
from shapely.geometry.polygon import Polygon, orient
from shapely.geometry import mapping, LineString, Point
from shapely.ops import cascaded_union, transform
from pybob.coreg_tools import dem_coregistration, false_hillshade, get_slope, create_stable_mask
from pybob.GeoImg import GeoImg
from pybob.image_tools import nanmedian_filter
from pybob.plot_tools import plot_shaded_dem
from mpl_toolkits.axes_grid1 import make_axes_locatable


def make_mask(inpoly, pts, raster_out=False):
    """
    Create a True/False mask to determine whether input points are within a polygon.
    
    Parameters
    ----------
    inpoly : shapely Polygon
        Input polygon to use to create mask.
    pts : array-like or GeoImg
        Either a list of (x,y) coordinates, or a GeoImg. If a list of (x,y) coordinates,
        make_mask uses shapely.polygon.contains to determine if the point is within inpoly.
        If pts is a GeoImg, make_mask uses ogr and gdal to "rasterize" inpoly to the GeoImg raster.
    raster_out : bool
        Kind of output to return. If True, pts must be a GeoImg.
        
    Returns
    -------
    mask : array-like, bool
        An array which is True where pts is within inpoly, False elsewhere.
    """
    if not raster_out:
        mask = []
        for pt in pts:
            mask.append(inpoly.contains(Point(pt)))
        return mask
    else:
        if type(pts) is not GeoImg:
            raise TypeError('raster_out is requested, but pts is not a GeoImg')
        # use gdal, ogr
        # first, create a layer for the polygon to live in.
        drv = ogr.GetDriverByName('Memory')
        dst_ds = drv.CreateDataSource('out')
        masklayer = dst_ds.CreateLayer('test', geom_type=ogr.wkbMultiPolygon, srs=pts.spatialReference)
        feature = ogr.Feature(masklayer.GetLayerDefn())
        feature.SetGeometry(ogr.CreateGeometryFromWkt(inpoly.wkt))
        #        feature.SetGeometry(ogr.CreateGeometryFromWkt(inpoly.crs_wkt))
        masklayer.CreateFeature(feature)
        feature.Destroy()
        # now, create the raster to burn the mask to.
        masktarget = gdal.GetDriverByName('MEM').Create('', pts.npix_x, pts.npix_y, 1, gdal.GDT_Byte)
        masktarget.SetGeoTransform((pts.xmin, pts.dx, 0, pts.ymax, 0, pts.dy))
        masktarget.SetProjection(pts.proj)
        masktarget.GetRasterBand(1).Fill(0)
        gdal.RasterizeLayer(masktarget, [1], masklayer)
        mask = masktarget.GetRasterBand(1).ReadAsArray()
        mask[mask != 0] = 1
        return mask == 1


def make_group_id(inmat, grpid):
    """
    Make a unique ID for statistical analysis.
    
    Parameters
    ----------
    inmat : array-like
        Input matrix to create ID for.
    grpid : array-like
        Matrix of input group IDs.

    Returns
    -------
    outmat : array-like
        Output of group IDs.
    """
    return np.floor(inmat / grpid) * grpid


def get_group_statistics(invar, indata, indist=500):
    """
    Calculate statistics on groups of pixels.
    """
    xxid = make_group_id(invar, indist)  # across track coordinates (grouped 500m)
    # yyid = make_group_id(yyr,500) # along track coordinates (grouped 500m)

    # Calculate group statistics 
    mykeep = np.isfinite(indata)
    data = pd.DataFrame({'dH': indata[mykeep], 'XX': xxid[mykeep]})
    xxgrp = data['dH'].groupby(data['XX']).describe()

    return xxgrp


def reproject_geometry(src_data, src_epsg, dst_epsg):
    """
    Reprojects src_data from one coordinate system to another.
    
    Parameters
    ----------
    src_data : geometry object
        shapely geometry object to be reprojected.
    src_epsg : int or str
        EPSG code for source CRS
    dst_epsg : int or str
        EPSG code for destination CRS
        
    Returns
    -------
    dst_data : geometry object
        reprojected shapely geometry object.    
    """
    src_proj = pyproj.Proj(init='epsg:{}'.format(src_epsg))
    dst_proj = pyproj.Proj(init='epsg:{}'.format(dst_epsg))
    project = partial(pyproj.transform, src_proj, dst_proj)
    return transform(project, src_data)


def nmad(data):
    """
    Calculate the Normalized Median Absolute Deviation (NMAD) of the input dataset.
    
    Parameters
    ----------
    data : array-like
        Dataset on which to calculate nmad
    
    Returns
    -------
    nmad : array-like
        Normalized Median Absolute Deviation of input dataset
    """
    return np.nanmedian(np.abs(data) - np.nanmedian(data))


def get_aster_footprint(gran_name, epsg='4326'):
    """
    Create shapefile of ASTER footprint from .met file.
    
    Parameters
    ----------
    gran_name : str
        ASTER granule name to use; assumed to also be the folder in which .met file(s) are stored.
    epsg : str or int
        EPSG code for coordinate system to use.
    Returns
    -------
    footprint : Polygon
        shapely Polygon representing ASTER footprint, re-projected to given geometry.
    """

    ### MADE AN ADJUSTMENT TO ASSUME THAT THE .MET FILE IS IN THE CURRENT FOLDER OF OPERATION
    metlist = glob(os.path.abspath('*.met'))

    schema = {'properties': [('id', 'int')], 'geometry': 'Polygon'}
    outshape = fiona.open(gran_name + '_Footprint.shp', 'w', crs=fiona.crs.from_epsg(int(epsg)),
                          driver='ESRI Shapefile', schema=schema)

    footprints = []
    for m in metlist:
        clean = [line.strip() for line in open(m).read().split('\n')]

        if os.path.sep in m:
            m = m.split(os.path.sep)[-1]

        latinds = [i for i, line in enumerate(clean) if 'GRingPointLatitude' in line]
        loninds = [i for i, line in enumerate(clean) if 'GRingPointLongitude' in line]

        latlines = clean[latinds[0]:latinds[1] + 1]
        lonlines = clean[loninds[0]:loninds[1] + 1]

        lonvalstr = lonlines[2]
        latvalstr = latlines[2]

        lats = [float(val) for val in latvalstr.strip('VALUE  = ()').split(',')]
        lons = [float(val) for val in lonvalstr.strip('VALUE  = ()').split(',')]

        coords = list(zip(lons, lats))
        footprints.append(Polygon(coords))

    footprint = cascaded_union(footprints)
    footprint = footprint.simplify(0.0001)
    outprint = reproject_geometry(footprint, 4326, epsg)
    outshape.write({'properties': {'id': 1}, 'geometry': mapping(outprint)})
    outshape.close()
    return outprint


def orient_footprint(fprint):
    """
    Orient ASTER footprint coordinates to be clockwise, with upper left coordinate first.
    
    Parameters
    ----------
    fprint : Polygon
        Footprint polygon to orient.
    
    Returns
    -------
    o_fprint : Polygon
        Re-oriented copy of input footprint.
    """
    # orient the footprint coordinates so that they are clockwise
    fprint = orient(fprint, sign=-1)
    x, y = fprint.boundary.coords.xy
    x = x[:-1]  # drop the last coordinate, which is a duplicate of the first
    y = y[:-1]
    # as long as the footprints are coming from the .met file, the upper left corner 
    # will be the maximum y value.
    upper_left = np.argmax(y)
    new_inds = range(upper_left, len(x)) + range(0, upper_left)
    return Polygon(list(zip(np.array(x)[new_inds], np.array(y)[new_inds])))


def get_track_angle(fprint, track_dist):
    """
    Calculate the angle made by the ASTER flight track from the footprint.
    
    Parameters
    ----------
    fprint : shapely Polygon
        Footprint of ASTER scene.
    track_dist : float
        Distance along flight track within scene to calculate angle.
        
    Returns
    -------
    track_angle : float
        Angle, in degrees, of flight track.
    """
    # orient the footprint
    fprint = orient_footprint(fprint)
    x, y = fprint.boundary.coords.xy
    # upper_left = np.argmax(y)
    upper_right = np.argmax(x)
    lower_right = np.argmin(y)
    lower_left = np.argmin(x)

    lside = range(len(x) - 1, lower_left - 1, -1)
    rside = range(upper_right, lower_right + 1)

    left_side = LineString(list(zip(np.array(x)[lside], np.array(y)[lside])))
    right_side = LineString(list(zip(np.array(x)[rside], np.array(y)[rside])))

    lproj = left_side.interpolate(track_dist)
    rproj = right_side.interpolate(track_dist)
    # get the angle of the line formed by connecting lproj, rproj
    dx = lproj.x - rproj.x
    dy = lproj.y - rproj.y

    return 90 + np.rad2deg(np.arctan(dx / dy))


def preprocess(mst_dem, slv_dem, glacmask=None, landmask=None, work_dir='.', out_dir='biasrem', pts=False):
    """
    Pre-process ASTER scene to enable cross- and along-track corrections. Co-registers the
    ASTER (slave) and external (master) DEMs/ICESat, and shifts the orthoimage and correlation mask
    based on the offset calculated. Results are saved by default in a folder called 'coreg' which
    is moved to the 'bias_removal' folder at the end of this function
    
    Parameters
    ----------
    mst_dem : string or GeoImg
        Path to filename or GeoImg dataset representing external "master" DEM.
    slv_dem : string or GeoImg
        Path to filename or GeoImg dataset representing ASTER DEM.
    glacmask : string, optional
        Path to shapefile representing points to exclude from co-registration
        consideration (i.e., glaciers).
    landmask : string, optional
        Path to shapefile representing points to include in co-registration
        consideration (i.e., stable ground/land).
    cwd : string, optional
        Working directory to use [Assumes '.'].
    out_dir : string, optional
        Output directory for the new coregistration folder    
    Returns
    -------
    mst_coreg : GeoImg
        GeoImg dataset represeting co-registered external "master" DEM.
    slv_coreg : GeoImg
        GeoImg dataset representing co-registered ASTER "slave" DEM.
    shift_params : tuple
        Tuple containing x, y, and z shifts calculated during co-regisration process.
    """
    # if the output directory does not exist, create it.
    # out_dir = os.path.sep.join([work_dir, out_dir])
    # print(out_dir)
    try:
        os.makedirs(out_dir)
    except OSError as exc:  # Python >2.5
        if exc.errno == errno.EEXIST and os.path.isdir(out_dir):
            pass
        else:
            raise
    # Get the ASTER names
    ast_name = slv_dem.rsplit('_Z.tif')[0]
    mask_name = os.path.sep.join([work_dir, '{}_CORR.tif'.format(ast_name)])
    slv_name = os.path.sep.join([work_dir, '{}_filtZ.tif'.format(ast_name)])
    # filter DEM using correlation mask !!! NEED to update to allow for either GeoIMG or pathname as INPUT
    mask_raster_threshold(slv_dem, mask_name, slv_name, 60, np.float32)

    # co-register master, slave
    coreg_dir = os.path.sep.join([out_dir, 'coreg'])
    mst_coreg, slv_coreg, shift_params = dem_coregistration(mst_dem, slv_name, glaciermask=glacmask,
                                                            landmask=landmask, outdir=coreg_dir, pts=pts)
    #    print(slv_coreg.filename)
    # remove coreg folder, save slv as *Zadj1.tif, but save output.pdf
    #    shutil.move(os.path.sep.join([out_dir, 'CoRegistration_Results.pdf']),
    #                os.path.sep.join([cwd, 'CoRegistration_Results.pdf']))
    #    shutil.move(os.path.sep.join([out_dir, 'coreg_params.txt']), os.path.sep.join([cwd, 'coreg_params.txt']))
    #    shutil.rmtree(out_dir)

    ### EXPORT and write files. Move directory to main Bias Corrections directory
    ### *** NOTE: GeoImg.write replaces the mask !!!***
    slv_coreg.write(os.path.sep.join([out_dir, '{}_Z_adj.tif'.format(ast_name)]))
    # shift ortho, corr masks, save as *adj1.tif
    ortho = GeoImg(ast_name + '_V123.tif')
    corr = GeoImg(ast_name + '_CORR.tif')

    ortho.shift(shift_params[0], shift_params[1])
    ortho.write(os.path.sep.join([out_dir, '{}_V123_adj.tif'.format(ast_name)]))

    corr.shift(shift_params[0], shift_params[1])
    corr.write(os.path.sep.join([out_dir, '{}_CORR_adj.tif'.format(ast_name)]), dtype=np.uint8)

    plt.close("all")

    return mst_coreg, slv_coreg, shift_params


def mask_raster_threshold(rasname, maskname, outfilename, threshold=50, datatype=np.float32):
    """
    filters values in rasname if maskname is less then threshold
    """
    myras = GeoImg(rasname)
    mymask = GeoImg(maskname)
    mymask.img[mymask.img < threshold] = 0

    rem_open = binary_opening(mymask.img, structure=disk(5))
    myras.img[~rem_open] = np.nan
    myras.write(outfilename, driver='GTiff', dtype=datatype)

    return myras


def RMSE(indata):
    """ Return root mean square of indata."""

    # add 3x std dev filter
    #    indata[np.abs(indata)>3*np.nanstd(indata)] = np.nan

    myrmse = np.sqrt(np.nanmean(np.square(np.asarray(indata))))
    return myrmse


def calculate_dH(mst_dem, slv_dem, pts):
    """
    # mst_dem and slv_dem must be GeoIMG objects with same pixels
    # 1) calculates the differences
    # 2) Runs a 5x5 pixel median filter
    # 3) Hard Removes values > 100
    """
    if not pts:
        zupdate = np.ma.array(mst_dem.img.data - slv_dem.img.data, mask=slv_dem.img.mask)
        #        zupdate2 = np.ma.array(ndimage.median_filter(zupdate, 7), mask=slv_dem.img.mask)
        zupdate2 = np.ma.array(nanmedian_filter(zupdate, size=7), mask=slv_dem.img.mask)
        dH = slv_dem.copy(new_raster=zupdate2)

        master_mask = isinstance(mst_dem.img, np.ma.masked_array)
        slave_mask = isinstance(slv_dem.img, np.ma.masked_array)

        myslope = get_slope(slv_dem)
        fmask = np.logical_or.reduce((np.greater(np.abs(dH.img), 100),
                                      np.less(myslope.img, 0.5),
                                      np.greater(myslope.img, 25)))
        # if we have two masked arrays, take the logical 'or' of those masks
        # and apply it to dH
        if master_mask and slave_mask:
            smask = np.logical_or(mst_dem.img.mask, slv_dem.img.mask)
        elif master_mask:
            smask = mst_dem.img.mask
        elif slave_mask:
            smask = slv_dem.img.mask
        else:  # needs to have an option where none of the above
            smask = np.zeros(dH.img.shape, dtype=bool)  # should be all false
        mask = np.logical_or(smask, fmask)
        dH.mask(mask)
    else:
        # NEED TO CHECK THE MASKING
        slave_pts = slv_dem.raster_points(mst_dem.xy, nsize=3, mode='cubic')
        dH = mst_dem.elev - slave_pts

        myslope = get_slope(slv_dem)
        slope_pts = myslope.raster_points(mst_dem.xy, nsize=3, mode='cubic')

        fmask = np.logical_or.reduce((np.greater(np.abs(dH), 100), np.less(slope_pts, 0.5), np.greater(slope_pts, 25)))

        dH[fmask] = np.nan
    return dH


def get_xy_rot(mst_dem, myang):
    # creates matrices for along and across track distances from a reference dem and a raster angle map (in radians)

    xx, yy = mst_dem.xy(grid=True)
    xx = xx - np.min(xx)
    yy = yy - np.min(yy)
    # xxr = np.multiply(xx,np.rad2deg(np.cos(myang))) + np.multiply(-1*yy,np.rad2deg(np.sin(myang)))
    # yyr = np.multiply(xx,np.rad2deg(np.sin(myang))) + np.multiply(yy,np.rad2deg(np.cos(myang)))
    xxr = np.multiply(xx, np.cos(myang)) + np.multiply(-1 * yy, np.sin(myang))
    yyr = np.multiply(xx, np.sin(myang)) + np.multiply(yy, np.cos(myang))

    # TO USE FOR INITIALIZING START AT ZERO
    xxr = xxr - np.nanmin(xxr)
    yyr = yyr - np.nanmin(yyr)

    plt.figure(figsize=(5, 5))
    plt.imshow(xxr, interpolation='nearest')
    # plt.show()

    plt.figure(figsize=(5, 5))
    plt.imshow(yyr, interpolation='nearest')
    # plt.show()

    return xxr, yyr


def get_atrack_coord(mst_dem, myangN, myangB):
    """ Creates numpy matrices for along and across track distances from MMASTER 
    dem and angle maps. 
    
    Parameters
    ----------
    mst_dem :   GeoImg of DEM
    myangN  :   GeoImg of MMASTER 3N track angle (in degrees by default)
    myangB  :   GeoImg of MMASTER 3B track angle (in degrees by default)

    Returns
    -------
    yyn : numpy array of along track distance in 3N track direction
    yyb : numpy array of along track distance in 3B track direction    
    """

    myangN = np.deg2rad(myangN.img)
    myangB = np.deg2rad(myangB.img)

    xx, yy = mst_dem.xy(grid=True)
    xx = xx - np.min(xx)
    yy = yy - np.min(yy)
    # xxr = np.multiply(xx,np.rad2deg(np.cos(myang))) + np.multiply(-1*yy,np.rad2deg(np.sin(myang)))
    # yyr = np.multiply(xx,np.rad2deg(np.sin(myang))) + np.multiply(yy,np.rad2deg(np.cos(myang)))
    # xxr = np.multiply(xx,np.cos(myang)) + np.multiply(-1*yy,np.sin(myang))

    yyn = np.multiply(xx, np.sin(myangN)) + np.multiply(yy, np.cos(myangN))
    yyb = np.multiply(xx, np.sin(myangB)) + np.multiply(yy, np.cos(myangB))

    yyn = yyn - np.nanmin(yyn)
    yyb = yyb - np.nanmin(yyb)
    plt.figure(figsize=(5, 5))
    plt.imshow(yyn, interpolation='nearest')
    # plt.show()

    plt.figure(figsize=(5, 5))
    plt.imshow(yyb, interpolation='nearest')
    # plt.show()

    return yyn, yyb


def get_fit_variables(mst_dem, slv_dem, xxn, pts, xxb=None):
    """
    prepare input variables for fitting. 
    """
    if not pts:
        dHmat = calculate_dH(mst_dem, slv_dem, pts)
        dH = dHmat.img.reshape((1, dHmat.img.size))
        xx = xxn.reshape((1, xxn.size))
        if xxb is not None:
            xx2 = xxb.reshape((1, xxb.size))
        masked = isinstance(dH, np.ma.masked_array)
        if masked:
            xx = np.ma.masked_array(xxn.reshape((1, xxn.size)), dH.mask)
            if xxb is not None:
                xx2 = np.ma.masked_array(xx2, dH.mask)
                xx2 = xx2.compressed()
            xx = xx.compressed()
            dH = dH.compressed()
    elif pts:
        XXR = slv_dem.copy(new_raster=xxn)
        xx = XXR.raster_points(mst_dem.xy, nsize=3, mode='cubic')
        dH = calculate_dH(mst_dem, slv_dem, pts)
        if xxb is not None:
            XXR2 = slv_dem.copy(new_raster=xxb)
            xx2 = XXR2.raster_points(mst_dem.xy, mode='cubic')
    # Mask and filter (remove outliers)
    mynan = np.logical_or.reduce((np.invert(np.isfinite(dH)),
                                  np.invert(np.isfinite(xx)),
                                  (np.abs(dH) > np.nanstd(dH) * 3)))

    dH = np.squeeze(dH[~mynan])
    xx = np.squeeze(xx[~mynan])

    # get group statistics of dH, and create matrix with same shape as orig_data
    grp_sts = get_group_statistics(xx, dH, indist=500)
    grp_xx = grp_sts.index.values
    grp_dH = grp_sts['50%'].values

    if xxb is None:
        xx2 = np.nan
    else:
        xx2 = np.squeeze(xx2[~mynan])

    return xx, dH, grp_xx, grp_dH, xx2


def fitfun_polynomial(xx, params):
    #    myval=0
    #    for i in np.arange(0,params.size):
    #        myval = myval + params[i]*(xx**i)
    # myval=myval + params[i]*(xx**i)
    return sum([p * (np.divide(xx, 1000) ** i) for i, p in enumerate(params)])


def robust_polynomial_fit(xx, yy):
    print("Original Sample size :", yy.size)
    # mykeep=np.isfinite(yy) and np.isfinite(xx)
    #    mykeep = np.logical_and(np.isfinite(yy), np.isfinite(xx))
    mykeep = np.logical_and.reduce((np.isfinite(yy), np.isfinite(xx), (np.abs(yy) < np.nanstd(yy) * 3)))
    xx = xx[mykeep]
    yy = yy[mykeep]

    print("Final Sample size :", yy.size)
    print("Remaining NaNs :", np.sum(np.isnan(yy)))
    sampsize = min(int(0.15 * xx.size), 25000)  # np.int(np.floor(xx.size*0.25))
    if xx.size > sampsize:
        mysamp = np.random.randint(0, xx.size, sampsize)
    else:
        mysamp = np.arange(0, xx.size)

    plt.figure(figsize=(7, 5), dpi=200)
    # fig.suptitle(title, fontsize = 14)
    plt.plot(xx[mysamp], yy[mysamp], '^', ms=1, color='0.5', rasterized=True, fillstyle='full')

    myorder = 6
    mycost = np.empty(myorder)
    coeffs = np.zeros((myorder, myorder + 1))
    xnew = np.arange(np.nanmin(xx), np.nanmax(xx), 1000)

    def errfun(p, xx, yy):
        return fitfun_polynomial(xx, p) - yy

    for deg in np.arange(1, myorder + 1):
        #        p0 = np.zeros(deg + 1)
        p0 = poly.polyfit(np.divide(xx[mysamp], 1000), yy[mysamp], deg)
        print("Initial Parameters: ", p0)
        #        lbb = np.zeros(deg + 1)-1000
        #        ubb = np.zeros(deg + 1)+1000

        #        myresults = optimize.least_squares(errfun, p0, args=(xx[mysamp], yy[mysamp]))
        myresults = optimize.least_squares(errfun, p0, args=(xx[mysamp], yy[mysamp]), method='trf', loss='soft_l1',
                                           f_scale=0.5, ftol=1E-8, xtol=1E-8)
        #        print("Status: ", myresults.status)
        print("Polynomial degree - ", deg, " --> Status: ", myresults.success, " - ", myresults.status)
        print(myresults.message)
        print("Lowest cost:", myresults.cost)
        print("Parameters:", myresults.x)

        mycost[deg - 1] = myresults.cost
        coeffs[deg - 1, 0:myresults.x.size] = myresults.x

        mypred = fitfun_polynomial(xnew, myresults.x)
        plt.plot(xnew, mypred)

    fidx = mycost.argmin()
    plt.ylim(-75, 75)

    # This is to check whether percent improvement is a better way to choose the best fit. 
    # For now, comment out... 
    #    perimp=np.divide(mycost[:-1]-mycost[1:],mycost[:-1])*100
    #    fmin=np.asarray(np.where(perimp>5))
    #    if fmin.size!=0:
    #        fidx = fmin[0,-1]+1
    # print('fidx: {}'.format(fidx))

    print("Polynomial Order Selected: ", fidx + 1)

    return np.trim_zeros(coeffs[fidx], 'b'), fidx + 1


def polynomial_fit(x, y):
    """
    [DEPRECATED] A polynomial search function for orders 1-6 given dependent (x) and independent (y)
    variables. Uses the numpy polynomial package. 
    """
    # edge removal
    #    x = x[5:-5]
    #    y = y[5:-5]

    max_poly_order = 6

    plt.figure(figsize=(7, 5))
    plt.plot(x, y, '.')
    rmse = np.empty(max_poly_order - 1)
    coeffs = np.zeros((max_poly_order - 1, max_poly_order + 1))
    xnew = np.arange(np.nanmin(x), np.nanmax(x), 1000)
    for deg in np.arange(2, max_poly_order + 1):
        # print(deg)
        p, r = poly.polyfit(x, y, deg, full=True)
        p2 = poly.polyval(xnew, p)
        plt.plot(xnew, p2)
        coeffs[deg - 2, 0:p.size] = p
        rmse[deg - 2] = np.sqrt(np.divide(r[0], y.size - deg))

    plt.figure(figsize=(7, 5))
    plt.plot(np.arange(2, max_poly_order + 1), rmse)

    # Choose order of polynomial - 2 options 
    # a) lowest RMSE or 
    # b) by checking the percent improvemnt (more robust?)

    # a) lowest RMSE
    fidx = rmse.argmin()

    # b) [DEFAULT??] find the highest order which gave a 5% improvemnt in the RMSE
    perimp = np.divide(rmse[:-1] - rmse[1:], rmse[:-1]) * 100
    # print(perimp)
    # print(rmse)
    fmin = np.asarray(np.where(perimp > 5))
    if fmin.size != 0:
        fidx = fmin[0, -1] + 1
    # print('fidx: {}'.format(fidx))

    print("Polynomial Order Selected: ", fidx + 2)
    return coeffs[fidx], rmse[fidx]


def fitfun_sumofsin(xx, p):
    """
    The Base function for the sum of sines fitting to elevation differences in the along track (xx) direction
    of the ASTER flightpath. DEPRECATED - uses only one angle instead of two. see fitfun_sumofsin_2angle
    """
    #    myval = 0
    #    for bb in np.arange(0, p.size - 1, 3):
    #        myval = myval + p[bb] * np.sin(p[bb + 1] * xx + p[bb + 2])
    #
    p = np.asarray(p)
    aix = np.arange(0, p.size, 3)
    bix = np.arange(1, p.size, 3)
    cix = np.arange(2, p.size, 3)
    #    if len(xx.shape) == 1:
    #        myval = np.sum(p[aix] * np.sin(p[bix] * xx[:,np.newaxis] + p[cix]),axis=1)
    #    elif len(xx.shape) == 2:
    #        myval = np.sum(p[aix] * np.sin(p[bix] * xx[:,:,np.newaxis] + p[cix]),axis=2)
    if len(xx.shape) == 1:
        myval = np.sum(p[aix] * np.sin(np.divide(2 * np.pi, p[bix]) * np.divide(xx[:, np.newaxis], 1000) + p[cix]),
                       axis=1)
    elif len(xx.shape) == 2:
        myval = np.sum(p[aix] * np.sin(np.divide(2 * np.pi, p[bix]) * np.divide(xx[:, :, np.newaxis], 1000) + p[cix]),
                       axis=2)

    return myval


def fitfun_sumofsin_2angle(xxn, xxb, p):
    p = np.squeeze(np.asarray(p))
    aix = np.arange(0, p.size, 6)
    bix = np.arange(1, p.size, 6)
    cix = np.arange(2, p.size, 6)
    dix = np.arange(3, p.size, 6)
    eix = np.arange(4, p.size, 6)
    fix = np.arange(5, p.size, 6)

    if len(xxn.shape) == 1:
        myval = np.sum(p[aix] * np.sin(np.divide(2 * np.pi, p[bix]) *
                                       np.divide(xxn[:, np.newaxis], 1000) +
                                       p[cix]) + p[dix] * np.sin(np.divide(2 * np.pi, p[eix]) *
                                                                 np.divide(xxb[:, np.newaxis], 1000) + p[fix]), axis=1)
    elif len(xxn.shape) == 2:
        myval = np.sum(p[aix] * np.sin(np.divide(2 * np.pi, p[bix]) *
                                       np.divide(xxn[:, :, np.newaxis], 1000) +
                                       p[cix]) + p[dix] * np.sin(np.divide(2 * np.pi, p[eix]) *
                                                                 np.divide(xxb[:, :, np.newaxis], 1000) + p[fix]), axis=2)
    return myval


def huber_loss(z):
    out = np.asarray(np.square(z) * 1.000)
    out[np.where(z > 1)] = 2 * np.sqrt(z[np.where(z > 1)]) - 1
    return out.sum()


def soft_loss(z):  # z is residual
    out = 2 * (np.sqrt(1 + z) - 1)  # SOFT-L1 loss function (reduce the weight of outliers)
    return out


def costfun_sumofsin(p, xxn, yy, xxb=None):
    if xxb is not None:
        myval = fitfun_sumofsin_2angle(xxn, xxb, p)
    else:
        myval = fitfun_sumofsin(xxn, p)
    # DEFINE THE COST FUNCTION
    #    myerr = RMSE(yy - myval)
    #    myerr = nmad(yy - myval)
    #    myerr = np.sum(np.abs(yy-myval))    # called MAE or L1 loss function
    #    myerr = np.linalg.norm(yy-myval)
    #    myerr = np.sqrt(np.sum((yy-myval) ** 2))
    #    myerr = huber_loss(yy-myval)    # HUBER loss function (reduce the weight of outliers)
    #    myerr = np.sum((np.sqrt(1+np.square(yy-myval))-1))    # SOFT-L1 loss function (reduce the weight of outliers)
    myscale = 0.5
    myerr = np.sum(np.square(myscale) * soft_loss(np.square(np.divide(yy - myval, myscale))))
    #    myerr = np.sum( np.square(myscale)*2*(np.sqrt(1+np.square(np.divide(yy-myval,myscale)))-1) ) 
    # SOFT-L1 loss function  with SCALING
    return myerr


def plot_bias(xx, dH, grp_xx, grp_dH, title, pp, pmod=None, smod=None, plotmin=None, txt=None):
    """
    data : original data as numpy array (:,2), x = 1col,y = 2col
    gdata : grouped data as numpy array (:,2)
    pmod,smod are two model options to plot, numpy array (:,1)
    """

    mykeep = np.isfinite(dH)
    xx = xx[mykeep]
    dH = dH[mykeep]
    sampsize = min(int(0.15 * xx.size), 25000)
    if xx.size > sampsize:
        mysamp = np.random.randint(0, xx.size, sampsize)
    else:
        mysamp = np.arange(0, xx.size)
    # mysamp = mysamp.astype(np.int64) #huh?

    # title = 'Cross'
    fig = plt.figure(figsize=(7, 5), dpi=200)
    fig.suptitle(title + 'track bias', fontsize=14)
    if plotmin is None:
        plt.plot(xx[mysamp], dH[mysamp], '^', ms=0.75, color='0.5', rasterized=True, fillstyle='full',
                 label="Raw [samples]")
        plt.plot(grp_xx, grp_dH, '-', ms=2, color='0.15', label="Grouped Median")
    else:
        plt.plot(grp_xx, grp_dH, '^', ms=1, color='0.5', rasterized=True, fillstyle='full', label="Grouped Median")

    if pmod is not None:
        plt.plot(grp_xx, pmod, 'r-', ms=2, label="Basic Fit")
    if smod is not None:
        plt.plot(grp_xx, smod, 'm-', ms=2, label="SumOfSines-Fit")

    plt.plot(grp_xx, np.zeros(grp_xx.size), 'k-', ms=1)

    plt.xlim(np.min(grp_xx), np.max(grp_xx))
    # plt.ylim(-200,200)
    ymin, ymax = plt.ylim((np.nanmean(dH[mysamp])) - 2 * np.nanstd(dH[mysamp]),
                          (np.nanmean(dH[mysamp])) + 2 * np.nanstd(dH[mysamp]))

    # plt.axis([0, 360, -200, 200])
    plt.xlabel(title + ' track distance [meters]')
    plt.ylabel('dH [meters]')
    plt.legend(loc=0)
    #    plt.legend(('Raw [samples]', 'Grouped Median', 'Fit'), loc=1)

    if txt is not None:
        plt.text(0.05, 0.15, txt, fontsize=12, fontweight='bold', color='black',
                 family='monospace', transform=plt.gca().transAxes)

    # plt.show()
    pp.savefig(fig, bbox_inches='tight', dpi=200)


def final_histogram(dH0, dH1, dH2, dHfinal, pp):
    fig = plt.figure(figsize=(7, 5), dpi=600)
    plt.title('Elevation difference histograms', fontsize=14)
    if isinstance(dH0, np.ma.masked_array):
        dH0 = dH0.compressed()
        dH1 = dH1.compressed()
        dH2 = dH2.compressed()
        dHfinal = dHfinal.compressed()
    dH0 = np.squeeze(np.asarray(dH0[ np.logical_and.reduce((np.isfinite(dH0), (np.abs(dH0) < np.nanstd(dH0) * 3)))]))
    dH1 = np.squeeze(np.asarray(dH1[ np.logical_and.reduce((np.isfinite(dH1), (np.abs(dH1) < np.nanstd(dH1) * 3)))]))
    dH2 = np.squeeze(np.asarray(dH2[ np.logical_and.reduce((np.isfinite(dH2), (np.abs(dH2) < np.nanstd(dH2) * 3)))]))
    dHfinal = np.squeeze(np.asarray(dHfinal[ np.logical_and.reduce((np.isfinite(dHfinal), (np.abs(dHfinal) < np.nanstd(dHfinal) * 3)))]))

    if dH0[np.isfinite(dH0)].size < 2000:
        mybins = 40
    else:
        mybins = 100

    j1, j2 = np.histogram(dH0[np.isfinite(dH0)], bins=mybins, range=(-60, 60))
    jj1, jj2 = np.histogram(dH1[np.isfinite(dH1)], bins=mybins, range=(-60, 60))
    jjj1, jjj2 = np.histogram(dH2[np.isfinite(dH2)], bins=mybins, range=(-60, 60))
    k1, k2 = np.histogram(dHfinal[np.isfinite(dHfinal)], bins=mybins, range=(-60, 60))

    stats0 = [np.nanmean(dH0), np.nanmedian(dH0), np.nanstd(dH0), RMSE(dH0)]
    stats_fin = [np.nanmean(dHfinal), np.nanmedian(dHfinal), np.nanstd(dHfinal), RMSE(dHfinal)]

    plt.plot(j2[1:], j1, 'k-', linewidth=2, label="original")
    plt.plot(jj2[1:], jj1, 'b-', linewidth=2, label="After X-track")
    plt.plot(jjj2[1:], jjj1, 'm-', linewidth=2, label="After A-track (low freq)")
    plt.plot(k2[1:], k1, 'r-', linewidth=2, label="After A-track (all freq)")

    plt.xlabel('Elevation difference [meters]')
    plt.ylabel('Number of samples')
    plt.xlim(-50, 50)

    # numwidth = max([len('{:.1f} m'.format(xadj)), len('{:.1f} m'.format(yadj)), len('{:.1f} m'.format(zadj))])
    plt.text(0.05, 0.90, 'Mean: ' + ('{:.1f} m'.format(stats0[0])),
             fontsize=8, fontweight='bold', color='black', family='monospace', transform=plt.gca().transAxes)
    plt.text(0.05, 0.85, 'Median: ' + ('{:.1f} m'.format(stats0[1])),
             fontsize=8, fontweight='bold', color='black', family='monospace', transform=plt.gca().transAxes)
    plt.text(0.05, 0.80, 'Std dev.: ' + ('{:.1f} m'.format(stats0[2])),
             fontsize=8, fontweight='bold', color='black', family='monospace', transform=plt.gca().transAxes)
    plt.text(0.05, 0.75, 'RMSE: ' + ('{:.1f} m'.format(stats0[3])),
             fontsize=8, fontweight='bold', color='black', family='monospace', transform=plt.gca().transAxes)

    plt.text(0.05, 0.60, 'Mean: ' + ('{:.1f} m'.format(stats_fin[0])),
             fontsize=8, fontweight='bold', color='red', family='monospace', transform=plt.gca().transAxes)
    plt.text(0.05, 0.55, 'Median: ' + ('{:.1f} m'.format(stats_fin[1])),
             fontsize=8, fontweight='bold', color='red', family='monospace', transform=plt.gca().transAxes)
    plt.text(0.05, 0.50, 'Std dev.: ' + ('{:.1f} m'.format(stats_fin[2])),
             fontsize=8, fontweight='bold', color='red', family='monospace', transform=plt.gca().transAxes)
    plt.text(0.05, 0.45, 'RMSE: ' + ('{:.1f} m'.format(stats_fin[3])),
             fontsize=8, fontweight='bold', color='red', family='monospace', transform=plt.gca().transAxes)

    plt.legend(loc=0)

    pp.savefig(fig, bbox_inches='tight', dpi=200)


def correct_cross_track_bias(mst_dem, slv_dem, inang, pp, pts=False):
    # calculate along/across track coordinates
    myang = np.deg2rad(inang.img)
    xxr, yyr = get_xy_rot(slv_dem, myang)  # across,along track coordinates calculated from angle map

    # arrange the dependent (dH) and independent variables (ANGLE) into vectors
    # ALSO, filters the dH > threshold (40), and provides grouped statistics... 
    xx, dH, grp_xx, grp_dH, _ = get_fit_variables(mst_dem, slv_dem, xxr, pts)

    # #
    # Need conditional to check for large enough sample size... ?
    # #

    # POLYNOMIAL FITTING - here using my defined robust polynomial fitting
    #    pcoef, myorder = robust_polynomial_fit(grp_xx, grp_dH)
    pcoef, myorder = robust_polynomial_fit(xx, dH)
    polymod = fitfun_polynomial(xx, pcoef)
    polymod_grp = fitfun_polynomial(grp_xx, pcoef)
    polyres = RMSE(dH - polymod)
    print("Cross track robust Polynomial RMSE (all data): ", polyres)

    #   # USING POLYFIT With ALL/GROUPED data
    #    pcoef, _ = polynomial_fit(grp_xx,grp_dH) #mean
    #    pcoef2, _ = polynomial_fit(xx,dH) # USE ALL DATA
    #    polymod=poly.polyval(xx,pcoef2)
    #    polyres=RMSE(dH-polymod)
    #    print("Cross track standard Polynomial RMSE (all data): ", polyres)

    mytext = "Polynomial order: " + np.str(myorder)
    plot_bias(xx, dH, grp_xx, grp_dH, 'Cross', pp, pmod=polymod_grp, txt=mytext)

    # Generate correction for DEM
    #    out_corr = poly.polyval(xxr, pcoef)
    out_corr = fitfun_polynomial(xxr, pcoef)

    # Correct DEM
    zupdate = np.ma.array(slv_dem.img + out_corr, mask=slv_dem.img.mask)  # shift in z
    slv_dem = slv_dem.copy(new_raster=zupdate)

    return slv_dem, out_corr, pcoef


def correct_along_track_bias(mst_dem, slv_dem, inangN, inangB, pp, pts):
    # calculate along/across track coordinates
    # myang = np.deg2rad(np.multiply(inang,np.multiply(dH,0)+1))# generate synthetic angle image for testing
    xxn_mat, xxb_mat = get_atrack_coord(slv_dem, inangN,
                                        inangB)  # across,along track coordinates calculated from angle map

    # arrange the dependent (dH) and independent variables (ANGLE) into vectors
    # ALSO, filters the dH > threshold (40), and provides grouped statistics... 
    xxn, dH, grp_xx, grp_dH, xxb = get_fit_variables(mst_dem, slv_dem, xxn_mat, pts, xxb=xxb_mat)
    # #
    # Need conditional to check for enough sample size... 
    # #

    yy = dH
    # updated to print only the non-nan size of xxn
    print("Original Sample Size: ", np.where(np.isfinite(xxn))[0].size)
    #    mykeep = np.isfinite(yy) & np.isfinite(xxn) & np.isfinite(xxb) & (np.abs(yy) < np.nanstd(yy) * 3)
    #    mykeep = np.asarray(np.isfinite(yy) and np.isfinite(xxn) and np.isfinite(xxb) and (np.abs(yy) < np.nanstd(yy) * 3))
    mykeep = np.logical_and.reduce((np.isfinite(yy), 
                                    np.isfinite(xxn), 
                                    np.isfinite(xxb), 
                                    (np.abs(yy) < np.nanstd(yy) * 2.5)))
    xxn = np.squeeze(xxn[mykeep])
    xxb = np.squeeze(xxb[mykeep])
    yy = np.squeeze(yy[mykeep])

    # should be the right size, since we've filtered out the nans in the above steps.
    print("Filtered Sample Size: ", xxn.size)
    # sampsize = np.int(np.floor(xx.size*0.25)) # for use as a percentage
    sampsize = min(int(0.15 * xxn.size), 25000)
    if xxn.size > sampsize:
        mysamp = np.random.randint(0, xxn.size, sampsize)
    else:
        mysamp = np.arange(0, xxn.size)
    
    fig = plt.figure(figsize=(7, 5), dpi=200)
    # fig.suptitle(title, fontsize = 14)
    plt.plot(xxn[mysamp], yy[mysamp], '^', ms=0.5, color='0.5', rasterized=True, fillstyle='full')

    # First define the bounds of the three sine wave coefficients to solve
    order = 3
    lb1 = [0, 55, 0] # long-wave amplitude, frequency, phase
    ub1 = [20, 140, 2 * np.pi] # 
    lb2 = [0, 20, 0] # mid-range
    ub2 = [15, 37, 2 * np.pi]
    lb3 = [0, 3.5, 0] # jitter
    ub3 = [3, 5.5, 2 * np.pi]

    lbb = np.concatenate((np.tile(lb1, 2 * order), np.tile(lb2, 2 * order), np.tile(lb3, 2 * order)))
    ubb = np.concatenate((np.tile(ub1, 2 * order), np.tile(ub2, 2 * order), np.tile(ub3, 2 * order)))
    p0 = np.divide(lbb + ubb, 2)

    # use these parameters, plus the grouped statistics, to get an initial estimate for the sum of sines fit
    print("Fitting smoothed data to find initial parameters.")
    init_args = dict(args=(grp_xx, grp_dH), method="L-BFGS-B", 
                     bounds=optimize.Bounds(lbb, ubb), options={"ftol": 1E-4})
    init_results = optimize.basinhopping(costfun_sumofsin, p0, disp=True,
                                         T=500, niter_success=20,
                                         minimizer_kwargs=init_args)
    init_results = init_results.lowest_optimization_result
    #    def errfun(p, xxn, xxb, yy):
    #        return fitfun_sumofsin_2angle(xxn, xxb, p) - yy
    #    myresults = optimize.least_squares(errfun, p0, args=(xxn[mysamp], xxb[mysamp], yy[mysamp]),
    #                                           method='trf', bounds=([lbb, ubb]), loss='soft_l1',
    #                                           f_scale=0.5, ftol=1E-8, xtol=1E-8, tr_solver='lsmr')

    #    myresults0 = optimize.minimize(fitfun_sumofsin_2angle2, p0, args=(xxn[mysamp], xxb[mysamp], yy[mysamp]),
    #                                  bounds=optimize.Bounds(lbb,ubb), method='L-BFGS-B',
    #                                  options={'maxiter': 1000,'maxfun':1000, 'ftol':1E-8})
    print("Sum of Sines Fitting using ", mysamp.size, "samples")
    minimizer_kwargs = dict(args=(xxn[mysamp], yy[mysamp], xxb[mysamp]),
                            method="L-BFGS-B",
                            bounds=optimize.Bounds(lbb, ubb),
                            options={"ftol": 1E-4})
    myresults = optimize.basinhopping(costfun_sumofsin, init_results.x, disp=True,
                                      T=500, niter_success=10,
                                      minimizer_kwargs=minimizer_kwargs)
    myresults = myresults.lowest_optimization_result

    xxn2 = np.linspace(np.min(xxn[mysamp]), np.max(xxn[mysamp]), grp_xx.size)
    xxb2 = np.linspace(np.min(xxb[mysamp]), np.max(xxb[mysamp]), grp_xx.size)
    #    mypred0 = fitfun_sumofsin_2angle(xxn2, xxb2, myresults0.x)
    mypred = fitfun_sumofsin_2angle(xxn2, xxb2, myresults.x)
    #    plt.plot(xxn2, mypred0, '-', ms=2, color='k', rasterized=True, fillstyle='full')
    plt.plot(xxn2, mypred, '-', ms=2, color='r', rasterized=True, fillstyle='full')
    sinmod = fitfun_sumofsin_2angle(xxn_mat, xxb_mat, myresults.x)

    ### GET ONLY LOWER FREQUENCY RESULTS
    acoeff = myresults.x[:-18]
    mypred2 = fitfun_sumofsin_2angle(xxn2, xxb2, acoeff)
    sinmod2 = fitfun_sumofsin_2angle(xxn_mat, xxb_mat, acoeff)

    # plot_bias(orig_data,grp_data,mytype,pp)
    plot_bias(xxn, yy, grp_xx, grp_dH, 'Along', pp, pmod=mypred2, smod=mypred)

    out_corr = np.reshape(sinmod, slv_dem.img.shape)
    out_corr2 = np.reshape(sinmod2, slv_dem.img.shape)

    fig = plt.figure(figsize=(7, 5), dpi=200)
    ax = plt.gca()
    # fig.suptitle(title, fontsize = 14)
    plt1 = plt.imshow(out_corr)
    plt1.set_clim(np.nanmin(out_corr), np.nanmax(out_corr))
    divider = make_axes_locatable(ax)
    cax = divider.append_axes("right", size="5%", pad=0.05)
    plt.colorbar(plt1, cax=cax)
    plt.tight_layout()
    pp.savefig(fig, bbox_inches='tight', dpi=200)

    # export slave dem with three constraing along track frequences
    zupdate = np.ma.array(slv_dem.img + out_corr, mask=slv_dem.img.mask)  # all frequencies
    zupdate2 = np.ma.array(slv_dem.img + out_corr2, mask=slv_dem.img.mask)  # low frequencies
    slv_dem = slv_dem.copy(new_raster=zupdate)
    slv_dem2 = slv_dem.copy(new_raster=zupdate2)

    return (slv_dem, out_corr, myresults.x), (slv_dem2, out_corr2, acoeff)


################################################################################################
def mmaster_bias_removal(mst_dem, slv_dem, glacmask=None, landmask=None,
                         pts=False, work_dir='.', out_dir='biasrem',
                         return_geoimg=True, write_log=False):
    """
    Removes cross track and along track biases from MMASTER DEMs. 

    Parameters
    ----------
    mst_DEM : string or GeoImg
        Path to filename or GeoImg dataset representing "master" DEM or ICESat.
    slv_DEM : string or GeoImg
        Path to filename or GeoImg dataset representing "slave" DEM (developed for ASTER).
    glacmask : string, optional
        Path to shapefile representing points to exclude from co-registration
        consideration (i.e., glaciers).
    landmask : string, optional
        Path to shapefile representing points to include in co-registration
        consideration (i.e., stable ground/land).
    pts : bool, optional
        If True, program assumes that masterDEM represents point data (i.e., ICESat),
        as opposed to raster data. Slope/aspect are then calculated from slaveDEM.
        masterDEM should be a string representing an HDF5 file continaing ICESat data.
    out_dir : string, optional
        Location to save bias removal outputs. [default to the current directory]
    return_geoimg : bool, optional
        Return GeoImg objects of the corrected slave DEM and the co-registered master DEM [True]
    write_log : bool, optional
        Re-direct stdout, stderr to a log file in the work directory [False]
    """
    orig_dir = os.getcwd()
    os.chdir(work_dir)
    
    if write_log:
        print(os.getcwd())
        logfile = open('mmaster_bias_correct_' + str(os.getpid()) + '.log', 'w')
        errfile = open('mmaster_bias_correct_' + str(os.getpid()) + '_error.log', 'w')
        sys.stdout = logfile
        sys.stderr = errfile
    # if the output directory does not exist, create it.
    # out_dir = os.path.sep.join([work_dir, out_dir])
    try:
        os.makedirs(out_dir)
    except OSError as exc:  # Python >2.5
        if exc.errno == errno.EEXIST and os.path.isdir(out_dir):
            pass
        else:
            raise

    # import angle data
    ang_mapN = GeoImg('TrackAngleMap_3N.tif')
    ang_mapB = GeoImg('TrackAngleMap_3B.tif')
    ang_mapNB = ang_mapN.copy(new_raster=np.array(np.divide(ang_mapN.img + ang_mapB.img, 2)))

    # pre-processing steps (co-registration,Correlation_masking)
    mst_coreg, slv_coreg, shift_params = preprocess(mst_dem, slv_dem, glacmask=glacmask, landmask=landmask,
                                                    work_dir='.', out_dir=out_dir, pts=pts)

    # OPEN and start the Results.pdf
    pp = PdfPages(os.path.sep.join([out_dir, 'BiasCorrections_Results.pdf']))

    ### Create the stable terrain masks
    stable_mask = create_stable_mask(slv_coreg, glacmask, landmask)
    fmaskpoly = get_aster_footprint(slv_dem.rsplit('_Z.tif')[0], epsg=slv_coreg.epsg)
    fmask = make_mask(fmaskpoly, slv_coreg, raster_out=True)

    ### PREPARE numpy masked arrays for .img data
    smask = np.logical_or.reduce((np.invert(fmask), stable_mask, np.isnan(slv_coreg.img)))
    slv_coreg.unmask()
    slv_coreg.mask(smask)
    if pts:
        mst_coreg.clean()
        stable_mask = slv_coreg.copy(new_raster=smask)
        mst_coreg.mask(stable_mask.raster_points(mst_coreg.xy) == 0)

    ### Create initial plot of where stable terrain is, including ICESat pts
    fig1 = plot_shaded_dem(slv_coreg)
    #    ax=fig1.gca()
    if pts:
        plt.plot(mst_coreg.x[~np.isnan(mst_coreg.elev)], mst_coreg.y[~np.isnan(mst_coreg.elev)], 'k.')
    #    divider = make_axes_locatable(ax)
    #    cax = divider.append_axes("right", size="5%", pad=0.05)
    #    plt.colorbar(fig1, cax=cax)
    pp.savefig(fig1, bbox_inches='tight', dpi=200)

    ### cross-track bias removal 
    slv_coreg_xcorr, xcorr, pcoef = correct_cross_track_bias(mst_coreg, slv_coreg, ang_mapNB, pp, pts=pts)

    ### along-track bias removal
    full_res, low_res = correct_along_track_bias(mst_coreg, slv_coreg_xcorr, ang_mapN, ang_mapB, pp, pts=pts)
    slv_coreg_xcorr_acorr, acorr, scoef = full_res
    slv_coreg_xcorr_acorr0, acorr2, scoef0 = low_res

    ### Calculate dH and statistics    
    dH0 = calculate_dH(mst_coreg, slv_coreg, pts)
    dH1 = calculate_dH(mst_coreg, slv_coreg_xcorr, pts)
    dH2 = calculate_dH(mst_coreg, slv_coreg_xcorr_acorr0, pts)
    dH_final = calculate_dH(mst_coreg, slv_coreg_xcorr_acorr, pts)
    
    ### mask dH for 
    if not pts:
        # Calculate initial differences
        mytitle = 'dH Initial'
        false_hillshade(dH0, mytitle, pp)

        # Calculate After Cross Track Changes
        mytitle = 'dH After Cross Track Corrections'
        false_hillshade(dH1, mytitle, pp)

        # Calculate After Cross Track Changes
        mytitle = 'dH After Low Frequence Along Track Corrections'
        false_hillshade(dH2, mytitle, pp)

        # Calculate post correction differences
        mytitle = 'dH After ALL Along Track Corrections'
        false_hillshade(dH_final, mytitle, pp)

        final_histogram(dH0.img, dH1.img, dH2.img, dH_final.img, pp)
    elif pts:
        # Calculate initial differences
        final_histogram(dH0, dH1, dH2, dH_final, pp)

    #### PREPARE OUTPUT - have to apply the corrections to the original, unfiltered slave DEM.
    # first, we apply the co-registration shift.
    orig_slv = GeoImg(slv_dem)
    orig_slv.shift(shift_params[0], shift_params[1])
    orig_slv.img = orig_slv.img + shift_params[2]
    
    # now, calculate and apply the cross-track correction
    myang = np.deg2rad(ang_mapNB.img)
    xxr, _ = get_xy_rot(orig_slv, myang)
    cross_correction = fitfun_polynomial(xxr, pcoef)
    orig_slv.img = orig_slv.img + cross_correction
    
    outname = os.path.splitext(slv_dem)[0] + "_adj_X.tif"
    orig_slv.write(outname, out_folder=out_dir)
    np.savetxt(os.path.sep.join([out_dir, 'params_CrossTrack_Polynomial.txt']), pcoef)
    plt.close("all")

    # now, calculate and apply the along-track corrections
    xxn_mat, xxb_mat = get_atrack_coord(orig_slv, ang_mapN, ang_mapB)

    sinmod_low = fitfun_sumofsin_2angle(xxn_mat, xxb_mat, scoef0)
    along_correction_low = np.reshape(sinmod_low, orig_slv.img.shape)
    orig_slv.img = orig_slv.img + along_correction_low
    
    outname = os.path.splitext(slv_dem)[0] + "_adj_XA.tif"
    orig_slv.write(outname, out_folder=out_dir)
    np.savetxt(os.path.sep.join([out_dir, 'params_AlongTrack_SumofSines_lowfreq.txt']), scoef0)
    plt.close("all")

    # finally, calculate and apply the full-frequency along-track correction.
    sinmod = fitfun_sumofsin_2angle(xxn_mat, xxb_mat, scoef)
    along_correction = np.reshape(sinmod, orig_slv.img.shape)
    orig_slv.img = orig_slv.img - along_correction_low + along_correction

    outname = os.path.splitext(slv_dem)[0] + "_adj_XAJ.tif"
    orig_slv.write(outname, out_folder=out_dir)
    np.savetxt(os.path.sep.join([out_dir, 'params_AlongTrack_SumofSines.txt']), scoef)
    plt.close("all")

    ### re-coregister
    print('Re-co-registering DEMs.')
    recoreg_outdir = os.path.sep.join([out_dir, 're-coreg'])
    mst_coreg, slv_adj_coreg, shift_params2 = dem_coregistration(mst_dem, slv_coreg_xcorr_acorr,
                                                                 glaciermask=glacmask, landmask=landmask,
                                                                 outdir=recoreg_outdir, pts=pts)

    orig_slv.shift(shift_params2[0], shift_params2[1])
    orig_slv.img = orig_slv.img + shift_params2[2]
    orig_slv.write(os.path.splitext(slv_dem)[0] + "_adj_XAJ_final.tif", out_folder=out_dir)

    plt.close("all")
    # clean-up 
    pp.close()
    print("Fin.")

    if return_geoimg:
        return slv_coreg_xcorr_acorr, mst_coreg

    if write_log:
        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__
        logfile.close()
        errfile.close()

    os.chdir(orig_dir)