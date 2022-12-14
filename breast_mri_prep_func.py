import os
import pydicom as dicom
import nibabel as nib
import numpy as np
from glob import glob
import subprocess
import logging
import yaml
import multiprocessing
from nipype.interfaces.dcm2nii import Dcm2niix
from nipype.interfaces.ants import Registration
from nipype.interfaces.ants import ApplyTransforms
from nipype.interfaces.fsl import BET
from nipype.interfaces.fsl.maths import ApplyMask
from nipype.interfaces.fsl import Eddy
from nipype.interfaces.fsl import DTIFit
from nipype.interfaces.fsl import ExtractROI
from nipype.interfaces.fsl import Merge
from nipype.interfaces.ants import N4BiasFieldCorrection
import json
import re
import csv
import shutil
from cnn_breast_mask import batch_mask


# set tensorflow logging to FATAL
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'  # 0 = INFO, 1 = WARN, 2 = ERROR, 3 = FATAL
logging.getLogger('tensorflow').setLevel(logging.FATAL)


# Set up logging
# takes work dir
# returns logger
def make_log(work_dir, repeat=False):
    if not os.path.isdir(work_dir):
        os.mkdir(work_dir)
    # make log file, append to existing
    idno = os.path.basename(work_dir)
    log_file = os.path.join(work_dir, idno + "_log.txt")
    if repeat:
        open(log_file, 'w').close()
    else:
        open(log_file, 'a').close()
    # make logger
    logger = logging.getLogger("my_logger")
    logger.setLevel(logging.DEBUG)  # should this be DEBUG?
    # set all existing handlers to null to prevent duplication
    logger.handlers = []
    # create file handler that logs debug and higher level messages
    fh = logging.FileHandler(log_file)
    fh.setLevel(logging.DEBUG)
    # create console handler with a higher log level
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    # create formatter and add it to the handlers
    formatterch = logging.Formatter('%(message)s')
    formatterfh = logging.Formatter("[%(asctime)s]  [%(levelname)s]:     %(message)s", "%Y-%m-%d %H:%M:%S")
    ch.setFormatter(formatterch)
    fh.setFormatter(formatterfh)
    # add the handlers to logger
    logger.addHandler(ch)
    logger.addHandler(fh)
    logger.propagate = False
    logger.info("####################### STARTING NEW LOG #######################")


# first check for series_dict to find the appropriate image series from the dicom folder
# matching strings format is [[strs to match AND], OR [strs to match AND]
# for NOT strings that are in all caps, the algorithm will ensure the series does not start with that string
def make_serdict(reg_atlas, dcm_dir, params):
    # load json file
    assert os.path.isfile(params), "Param file does not exist at " + params
    with open(params, 'r') as f:
        json_str = f.read()
    sdict = json.loads(json_str)

    # handle atlas option for registration target
    for key in sdict:
        if 'reg_target' in sdict[key].keys():
            if sdict[key]['reg_target'] == 'atlas':
                sdict[key].update({'reg_target': reg_atlas})

    # add info section to series dict
    sdict.update({"info": {
        "filename": "None",
        "dcmdir": dcm_dir,
        "id": os.path.basename(os.path.dirname(dcm_dir)),
    }})

    return sdict


# unzip dicom directory
def unzip_file(dicom_zip):
    # logging
    logger = logging.getLogger("my_logger")
    # unzip and get out_dir, tmp_dir, dicom_dir
    acc_no = dicom_zip.rsplit("/", 1)[1].split(".", 1)[0]
    tmp_dir = os.path.join(os.path.dirname(dicom_zip), acc_no)
    logger.info("UNZIPPING:")
    logger.info("- " + dicom_zip + ". If files are already unzipped, work will not be repeated.")
    unz_cmd = "unzip -n -qq -d " + tmp_dir + " " + dicom_zip
    _ = subprocess.call(unz_cmd, shell=True)
    dicomdir = glob(tmp_dir + "/*/")
    dicomdir = dicomdir[0].rsplit("/", 1)[0]  # must remove trailing slash so that os.path.dirname returns one dir up
    # print some stating info
    logger.info("- Working directory = " + os.path.dirname(dicomdir))
    return dicomdir


# get all dicoms within a directory tree
def dcm_find(parent_dir):
    dcm_list = []
    for root, dirnames, filenames in os.walk(parent_dir):
        for filename in filenames:
            # if extension is dcm or there is no extension then test if it is a dicom
            if filename.endswith('.dcm'):
                dcm_list.append(os.path.join(root, filename))
            # if there is no file extension check if it is a dicom
            elif len(filename.split('.')) == 1 and not filename.lower() == 'dicomdir':
                try:
                    _ = dicom.read_file(os.path.join(root, filename))
                    dcm_list.append(os.path.join(root, filename))
                except:
                    pass
    return dcm_list


# function to get complete series list from dicom directory
def get_series(dicom_dir, repeat=False):
    # logging
    logger = logging.getLogger("my_logger")
    logger.info("GETTING SERIES LIST:")
    logger.info("- DICOM directory = " + dicom_dir)
    # indo prep
    idno = os.path.basename(os.path.dirname(dicom_dir))
    # define variables
    dicoms = []
    hdrs = []
    series = []

    # find all dicom files in dicom directory
    dcm_list = dcm_find(dicom_dir)

    # list directories
    dirs = sorted(list(set([os.path.dirname(it) for it in dcm_list])))

    # get lists of all dicom series
    for ind, direc in enumerate(dirs):
        dicoms.append([it for it in dcm_list if it.startswith(direc)])
        hdrs.append(dicom.read_file(dicoms[ind][0]))
        # add extra text to decription of postcontrast series indicating contrast was given
        # only if contrast agent is specified and is not empty
        con_str = ' '
        if hasattr(hdrs[ind], 'ContrastBolusAgent'):
            # occasionally the ContrastBolusAgent field will have "No" or "None" written in
            if not str(hdrs[ind].ContrastBolusAgent).lower().replace(' ', '') in ['', 'no', 'none', 'off']:
                con_str = ' postcon '
        # add extra text to description of reformatted series indicating reformat
        if hasattr(hdrs[ind], 'ImageType'):
            if 'REFORMATTED' in hdrs[ind].ImageType:
                con_str = con_str + 'ref '
        # if there is a series description then add it to the list
        if hasattr(hdrs[ind], 'SeriesDescription'):
            if hdrs[ind].SeriesDescription:
                series.append(str(hdrs[ind].SeriesDescription) + con_str + "[dir=" + direc[-5:] + "]")
        else:
            logger.info("- Skipping series " + str(ind + 1) + " without series description")
            series.append("none" + con_str + "[dir=" + direc[-5:] + "]")

    # save series list
    series_file = os.path.join(os.path.dirname(dicom_dir), idno + "_series_list.txt")
    if not os.path.isfile(series_file) or repeat:
        fileout = open(series_file, 'w')
        for ind, item in enumerate(series):
            fileout.write("%s" % item)
            nspaces = 75 - len(item)  # assume no series description longer than 75
            fileout.write("%s" % " " * nspaces)
            # Slice thickness, rounded to 3 decimal places
            fileout.write("%s" % "\tslthick=")
            try:
                fileout.write("%s" % str(round(float(hdrs[ind].SliceThickness), 3)))
            except Exception:
                fileout.write("%s" % "None")
            # Acquisition matrix
            fileout.write("%s" % "\tacqmtx=")
            try:
                fileout.write("%s" % str(hdrs[ind].AcquisitionMatrix[:4]))  # no more than four entries
            except Exception:
                fileout.write("%s" % "None\t")
            # rows x columns
            fileout.write("%s" % "\trowcol=")
            try:
                if len(str(hdrs[ind].Rows) + "x" + str(hdrs[ind].Columns)) <= 7:
                    fileout.write("%s" % str(hdrs[ind].Rows) + "x" + str(hdrs[ind].Columns))
                else:
                    fileout.write("%s" % ">1Kx1K")
            except Exception:
                fileout.write("%s" % "None")
            # slices
            fileout.write("%s" % "\tslices=")
            try:
                fileout.write("%s" % str(hdrs[ind].ImagesInAcquisition))
            except Exception:
                fileout.write("%s" % "None")
            # acquisition time rounded to nearest int
            fileout.write("%s" % "\tacqtime=")
            try:
                fileout.write("%s" % str(round(hdrs[ind].AcquisitionTime)))
            except Exception:
                fileout.write("%s" % "None")
            # contrast agent removing redundant spaces
            fileout.write("%s" % "\tcontrast=")
            try:
                fileout.write("%s" % " ".join(str(hdrs[ind].ContrastBolusAgent).split()))
            except Exception:
                fileout.write("%s" % "None")
            fileout.write("%s" % "\n")

    return dicoms, hdrs, series, dirs


# function to get filter substring matches by criteria, returns a list of indices for matching series
def substr_list(strings, substrs, substrnot):
    # logging
    logger = logging.getLogger("my_logger")
    # define variables
    inds = []
    # for each input series description
    number = 1
    for ind, string in enumerate(strings, 0):
        match = False
        # for each substr list in substrs
        for substrlist in substrs:
            # match strings using regex
            # first make sure all strings in a given substring list match the target string
            if all([re.search(item, string) for item in substrlist]):
                # then make sure none of the not strings match the target string
                if not any([re.search(item2, string) for item2 in substrnot]):
                    # only add new unique inds to the matching ind list
                    if ind not in inds:
                        inds.append(ind)
                        match = True
        # report matches
        if match:
            logger.info("- Matched series: " + string + " (" + str(number) + ")")
            number = number + 1  # step the number of matching series
    return inds


# get filtered series list
def filter_series(dicoms, hdrs, series, dirs, srs_dict):
    # logging
    logger = logging.getLogger("my_logger")
    # define variables
    new_dicoms = []
    new_hdrs = []
    new_series = []
    new_dirs = []
    # for each output, find match and append to new list for conversion
    for srs in srs_dict:
        # only search if terms are provided
        if "or" in srs_dict[srs].keys() and "not" in srs_dict[srs].keys():
            logger.info("FINDING SERIES: " + srs)
            inds = substr_list(series, srs_dict[srs]["or"], srs_dict[srs]["not"])  # calls above function
            # if there are inds of matches, pick the first match by default and check for more slices or repeat series
            if inds or inds == 0:  # if only 1 ind is passed, then use it (set keeper to the only ind)
                keeper = []
                number = 'None'
                if len(inds) == 1:
                    keeper = inds[0]
                    number = 1  # series number chosen to report in the logger info
                else:
                    # if more than one series found, first check for redo/repeat
                    redo_inds = []
                    for x in inds:
                        if any(item in series[x].lower() for item in ["repeat", "redo"]):
                            redo_inds.append(x)
                    if redo_inds:
                        inds = redo_inds
                    # if more than 1 inds, try to find the one with the most slices, otherwise just pick first one
                    for n, i in enumerate(inds, 1):
                        if n == 1:  # pick first ind by default
                            keeper = i
                            number = n
                        if hasattr(hdrs[i], "ImagesInAcquisition") and hasattr(hdrs[keeper], "ImagesInAcquisition"):
                            # if another series has more images, pick it instead
                            if int(hdrs[i].ImagesInAcquisition) > int(hdrs[keeper].ImagesInAcquisition):
                                keeper = i
                                number = n
                            # if another series has the same # images:
                            elif int(hdrs[i].ImagesInAcquisition) == int(hdrs[keeper].ImagesInAcquisition):
                                # and was acquired later, keep it instead
                                if hasattr(hdrs[i], "AcquisitionTime") and hasattr(hdrs[keeper], "AcquisitionTime"):
                                    try:
                                        if float(hdrs[i].AcquisitionTime) > float(hdrs[keeper].AcquisitionTime):
                                            keeper = i
                                            number = n
                                    except:
                                        pass

                # Report keeper series
                inds = keeper  # replace inds with just the keeper index
                logger.info("- Keeping series: " + series[inds] + " (" + str(number) + ")")
                new_dicoms.append(dicoms[inds])
                srs_dict[srs].update({"dicoms": dicoms[inds]})
                new_hdrs.append(hdrs[inds])
                srs_dict[srs].update({"hdrs": hdrs[inds]})
                new_series.append(series[inds])
                srs_dict[srs].update({"series": series[inds]})
                new_dirs.append(dirs[inds])
                srs_dict[srs].update({"dirs": dirs[inds]})
            else:
                logger.info("- No matching series found!")
                new_dicoms.append([])
                srs_dict[srs].update({"dicoms": []})
                new_hdrs.append([])
                srs_dict[srs].update({"hdrs": []})
                new_series.append("None")
                srs_dict[srs].update({"series": "None"})
                new_dirs.append("None")
                srs_dict[srs].update({"dirs": []})
    return srs_dict


# define function to convert selected dicoms
def dcm_list_2_niis(strs_dict, dicom_dir, repeat=False):
    # logging
    logger = logging.getLogger("my_logger")
    # id prep
    idno = strs_dict["info"]["id"]
    # allocate vars
    output_ser = []
    logger.info("CONVERTING FILES:")
    # basic converter initiation
    converter = Dcm2niix()
    converter.inputs.bids_format = False
    converter.inputs.single_file = True
    converter.inputs.args = '-w 2'
    converter.inputs.compress = "y"
    converter.inputs.output_dir = os.path.dirname(dicom_dir)
    converter.terminal_output = "allatonce"
    converter.anonymize = True
    # placeholder list for extra files created during conversion
    extras = []

    # convert all subdirectories from dicom to nii
    for series in strs_dict:
        convert_flag = False
        if "dicoms" in strs_dict[series].keys():
            converter.inputs.source_names = strs_dict[series]["dicoms"]
            outfilename = idno + "_" + series
            converter.inputs.out_filename = outfilename
            outfilepath = os.path.join(os.path.dirname(dicom_dir), outfilename + ".nii.gz")

            # handle case where output file does not exist yet and pre-reqs are present
            if not os.path.isfile(outfilepath) and not strs_dict[series]["series"] == "None":
                logger.info("- Converting " + outfilename)
                logger.debug(converter.cmdline)
                result = converter.run()
                convert_flag = True
                # make sure that file wasnt named something else during conversion
                if isinstance(result.outputs.converted_files, list):
                    converted = result.outputs.converted_files[0]
                    extras = result.outputs.converted_files.remove(result.outputs.converted_files[0])
                    if not extras:
                        extras = []
                else:
                    converted = result.outputs.converted_files
                # handle case where converted file is undefined
                if not converted:
                    converted = outfilepath
                if not converted == outfilepath:
                    logger.info("- " + series + " converted file is named " + os.path.basename(converted) +
                                ", renaming " + os.path.basename(outfilepath))
                    os.rename(converted, outfilepath)
                # identify any extra files generated during conversion
                more_extras = glob(str(outfilepath.rsplit('.nii.gz', 1)[0]) + '*.nii.gz').remove(outfilepath)
                if more_extras:
                    extras = list(set(extras + more_extras))
            else:
                # handle cases where output file already exists but repeat is true
                if os.path.isfile(outfilepath) and repeat:
                    logger.info("- " + outfilename + " already exists, but repeat is True, so it will be overwritten")
                    logger.info("- Converting " + outfilename)
                    logger.debug(converter.cmdline)
                    result = converter.run()
                    convert_flag = True
                    # make sure that file wasnt named something else during conversion, if so, rename to expected name
                    if isinstance(result.outputs.converted_files, list):
                        converted = sorted(result.outputs.converted_files)[0]
                    else:
                        converted = result.outputs.converted_files
                    # handle case where converted files is undefined
                    if not converted:
                        converted = outfilepath
                    if not converted == outfilepath:
                        logger.info("- " + series + " converted file is " + converted + ", renaming to " + outfilepath)
                        os.rename(converted, outfilepath)
                    # identify any extra files generated during conversion
                    more_extras = glob(str(outfilepath.rsplit('.nii.gz', 1)[0]) + '*.nii.gz')
                    extras = list(set(extras + more_extras))

                # handle case where outfile aready exists and repeat is false, or where prerequisites don't exist
                if os.path.isfile(outfilepath) and not repeat:
                    logger.info("- " + outfilename + " already exists and will not be overwritten")
                if not os.path.isfile(outfilepath) and strs_dict[series]["series"] == "None":
                    logger.info("- No existing file and no matching series found: " + series)

            # after running through conversion process, check if output file actually exists and update series dict
            if os.path.isfile(outfilepath):
                # if output file exists, regardless of whether created or not append name to outfile list
                output_ser.append(outfilepath)
                strs_dict[series].update({"filename": outfilepath})
                # handle options for post-coversion nifti processing here (only if output file is newly created)
                if convert_flag and any([item in strs_dict[series].keys() for item in ['split', 'split_func']]):
                    # handle use of a custom split function for splitting data
                    # this is where split_asl and combine_dti55 functions are used via finding it in the globals
                    if 'split_func' in strs_dict[series].keys():
                        if strs_dict[series]['split_func'] in globals():
                            globals()[strs_dict[series]['split_func']](strs_dict)
                    else:  # if not using custom split function, split based on split_multiphase
                        outnames = split_multiphase(outfilepath, strs_dict[series]['split'], series, repeat=False)
                        if outnames:
                            for k in outnames.keys():
                                # if series does not already exist in series list then it will not be updated
                                if k in strs_dict.keys():
                                    strs_dict[k].update({"filename": outnames[k]})

    # after all conversion and splitting is done, remove any extra files that may have been created
    if extras:
        for item in extras:
            if os.path.isfile(item):
                logger.info("- Removing extra file generated during conversion: " + item)
                os.remove(item)
    # print outputs of file conversion
    logger.info("CONVERTED FILES LIST:")
    for ser in strs_dict:
        if "dicoms" in strs_dict[ser] and "filename" in strs_dict[ser] and os.path.isfile(strs_dict[ser]["filename"]):
            logger.info("- " + ser + " = " + strs_dict[ser]["filename"])

    # handle special case of setting ASL registration moving image to ASL_anat when it was not newly created
    if "ASL" in list(strs_dict.keys()) and "filename" in list(strs_dict["ASL"].keys()):
        aslperf = strs_dict["ASL"]["filename"]
        anat_outname = aslperf.rsplit(".nii", 1)[0] + "_anat.nii.gz"
        if os.path.isfile(anat_outname):
            strs_dict["ASL"].update({"reg_moving": anat_outname})

    return strs_dict


# Split multiphase
# takes a multiphase (or other 4D) nifti and splits into one or more additional series based on options
def split_multiphase(nii_in, options, series, repeat=False):
    # setup return variable
    outnames = {}
    # logging
    logger = logging.getLogger("my_logger")
    # path prep
    basepath = nii_in.rsplit('_', 1)[1]
    # first check if all desired outputs already exist, if so, don't do any work
    if not repeat and all([os.path.isfile(os.path.join(basepath, ser + '.nii.gz')) for ser in options.keys()]):
        logger.info("- Split option was specified for " + series + " but split outputs already exist")
        for k in options.keys():
            outnames.update({k: os.path.join(basepath, k + '.nii.gz')})
        return outnames
    else:
        # data loading
        nii = nib.load(nii_in)
        data = nii.get_fdata()
        # if data is not 4D, then return
        if len(data.shape) < 4 or data.shape[3] < 2:
            logger.info("- Split option was specified for " + series +
                        ", and split outputs do not exist, but data is not 4D")
            return outnames
        # loop through splitting options - THIS WILL OVERWRITE OTHER SERIES
        for k in options.keys():
            dirname = os.path.dirname(nii_in)
            basename = os.path.basename(nii_in)
            outname = os.path.join(dirname, basename.split('_')[0] + '_' + k + '.nii.gz')
            logger.info("- Splitting " + series + " phase " + str(options[k]) + " as " + k)
            new_data = data[:, :, :, options[k]]
            new_nii = nib.Nifti1Image(new_data, nii.affine)
            nib.save(new_nii, outname)
            outnames.update({k: outname})
        return outnames


# Fast ants affine
# takes moving and template niis and a work dir
# performs fast affine registration and returns a list of transforms
def affine_reg(moving_nii, template_nii, work_dir, option=None, repeat=False):
    # logging
    logger = logging.getLogger("my_logger")
    # get basenames
    moving_name = os.path.basename(moving_nii).split(".")[0]
    template_name = os.path.basename(template_nii).split(".")[0]
    outprefix = os.path.join(work_dir, moving_name + "_2_" + template_name + "_")

    # registration setup
    antsreg = Registration()
    antsreg.inputs.args = '--float'
    antsreg.inputs.fixed_image = template_nii
    antsreg.inputs.moving_image = moving_nii
    antsreg.inputs.output_transform_prefix = outprefix
    antsreg.inputs.num_threads = multiprocessing.cpu_count()
    antsreg.inputs.smoothing_sigmas = [[6, 4, 1, 0], [6, 4, 1, 0]]
    antsreg.inputs.sigma_units = ['mm', 'mm']
    antsreg.inputs.transforms = ['Rigid', 'Affine']
    antsreg.terminal_output = 'none'
    antsreg.inputs.use_histogram_matching = True
    antsreg.inputs.write_composite_transform = True
    if isinstance(option, dict) and "reg_com" in option.keys():
        antsreg.inputs.initial_moving_transform_com = option["reg_com"]
    else:
        antsreg.inputs.initial_moving_transform_com = 1  # use center of mass for initial transform by default
    antsreg.inputs.winsorize_lower_quantile = 0.005
    antsreg.inputs.winsorize_upper_quantile = 0.995
    antsreg.inputs.metric = ['Mattes', 'Mattes']
    antsreg.inputs.metric_weight = [1.0, 1.0]
    antsreg.inputs.number_of_iterations = [[1000, 1000, 1000, 1000], [1000, 1000, 1000, 1000]]
    antsreg.inputs.convergence_threshold = [1e-07, 1e-07]
    antsreg.inputs.convergence_window_size = [10, 10]
    antsreg.inputs.radius_or_number_of_bins = [32, 32]
    antsreg.inputs.sampling_strategy = ['Regular', 'Regular']
    antsreg.inputs.sampling_percentage = [0.25, 0.25]  # 1
    antsreg.inputs.shrink_factors = [[4, 3, 2, 1], [4, 3, 2, 1]]
    antsreg.inputs.transform_parameters = [(0.1,), (0.1,)]

    trnsfm = outprefix + "Composite.h5"
    if not os.path.isfile(trnsfm) or repeat:
        logger.info("- Registering image " + moving_nii + " to " + template_nii)
        logger.debug(antsreg.cmdline)
        antsreg.run()
    else:
        logger.info("- Warp file already exists at " + trnsfm)
        logger.debug(antsreg.cmdline)
    return trnsfm


# Faster ants affine
# takes moving and template niis and a work dir
# performs fast affine registration and returns a list of transforms
def fast_affine_reg(moving_nii, template_nii, work_dir, option=None, repeat=False):
    # logging
    logger = logging.getLogger("my_logger")
    # get basenames
    moving_name = os.path.basename(moving_nii).split(".")[0]
    template_name = os.path.basename(template_nii).split(".")[0]
    outprefix = os.path.join(work_dir, moving_name + "_2_" + template_name + "_")

    # registration setup
    antsreg = Registration()
    antsreg.inputs.args = '--float'
    antsreg.inputs.fixed_image = template_nii
    antsreg.inputs.moving_image = moving_nii
    antsreg.inputs.output_transform_prefix = outprefix
    antsreg.inputs.num_threads = multiprocessing.cpu_count()
    antsreg.inputs.smoothing_sigmas = [[6, 4, 1], [6, 4, 1]]
    antsreg.inputs.sigma_units = ['mm', 'mm']
    antsreg.inputs.transforms = ['Rigid', 'Affine']
    antsreg.terminal_output = 'none'
    antsreg.inputs.use_histogram_matching = True
    antsreg.inputs.write_composite_transform = True
    if isinstance(option, dict) and "reg_com" in option.keys():
        antsreg.inputs.initial_moving_transform_com = option["reg_com"]
    else:
        antsreg.inputs.initial_moving_transform_com = 1  # use center of mass for initial transform by default
    antsreg.inputs.winsorize_lower_quantile = 0.005
    antsreg.inputs.winsorize_upper_quantile = 0.995
    antsreg.inputs.metric = ['Mattes', 'Mattes']
    antsreg.inputs.metric_weight = [1.0, 1.0]
    antsreg.inputs.number_of_iterations = [[1000, 1000, 1000], [1000, 1000, 1000]]
    antsreg.inputs.convergence_threshold = [1e-04, 1e-04]
    antsreg.inputs.convergence_window_size = [5, 5]
    antsreg.inputs.radius_or_number_of_bins = [32, 32]
    antsreg.inputs.sampling_strategy = ['Regular', 'Regular']
    antsreg.inputs.sampling_percentage = [0.25, 0.25]
    antsreg.inputs.shrink_factors = [[6, 4, 2], [6, 4, 2]] * 2
    antsreg.inputs.transform_parameters = [(0.1,), (0.1,)]

    trnsfm = outprefix + "Composite.h5"
    if not os.path.isfile(trnsfm) or repeat:
        logger.info("- Registering image " + moving_nii + " to " + template_nii)
        logger.debug(antsreg.cmdline)
        antsreg.run()
    else:
        logger.info("- Warp file already exists at " + trnsfm)
        logger.debug(antsreg.cmdline)
    return trnsfm


# Fast ants diffeomorphic registration
# takes moving and template niis and a work dir
# performs fast diffeomorphic registration and returns a list of transforms
def diffeo_reg(moving_nii, template_nii, work_dir, option=None, repeat=False):
    # logging
    logger = logging.getLogger("my_logger")
    # get basenames
    moving_name = os.path.basename(moving_nii).split(".")[0]
    template_name = os.path.basename(template_nii).split(".")[0]
    outprefix = os.path.join(work_dir, moving_name + "_2_" + template_name + "_")

    # registration setup
    antsreg = Registration()
    antsreg.inputs.args = '--float'
    antsreg.inputs.fixed_image = template_nii
    antsreg.inputs.moving_image = moving_nii
    antsreg.inputs.output_transform_prefix = outprefix
    antsreg.inputs.num_threads = multiprocessing.cpu_count()
    antsreg.terminal_output = 'none'
    if isinstance(option, dict) and "reg_com" in option.keys():
        antsreg.inputs.initial_moving_transform_com = option["reg_com"]
    else:
        antsreg.inputs.initial_moving_transform_com = 1  # use center of mass for initial transform by default
    antsreg.inputs.winsorize_lower_quantile = 0.005
    antsreg.inputs.winsorize_upper_quantile = 0.995
    antsreg.inputs.shrink_factors = [[4, 3, 2, 1], [8, 4, 2, 1], [4, 2, 1]]
    antsreg.inputs.smoothing_sigmas = [[6, 4, 1, 0], [4, 2, 1, 0], [2, 1, 0]]
    antsreg.inputs.sigma_units = ['mm', 'mm', 'mm']
    antsreg.inputs.transforms = ['Rigid', 'Affine', 'SyN']
    antsreg.inputs.use_histogram_matching = [True, True, True]
    antsreg.inputs.write_composite_transform = True
    antsreg.inputs.metric = ['Mattes', 'Mattes', 'Mattes']
    antsreg.inputs.metric_weight = [1.0, 1.0, 1.0]
    antsreg.inputs.number_of_iterations = [[1000, 1000, 1000, 1000], [1000, 1000, 1000, 1000], [250, 100, 50]]
    antsreg.inputs.convergence_threshold = [1e-07, 1e-07, 1e-07]
    antsreg.inputs.convergence_window_size = [5, 5, 5]
    antsreg.inputs.radius_or_number_of_bins = [32, 32, 32]
    antsreg.inputs.sampling_strategy = ['Regular', 'Regular', 'None']  # 'None'
    antsreg.inputs.sampling_percentage = [0.25, 0.25, 1]
    antsreg.inputs.transform_parameters = [(0.1,), (0.1,), (0.1, 3.0, 0.0)]

    trnsfm = outprefix + "Composite.h5"
    if not os.path.isfile(trnsfm) or repeat:
        logger.info("- Registering image " + moving_nii + " to " + template_nii)
        logger.debug(antsreg.cmdline)
        antsreg.run()
    else:
        logger.info("- Warp file already exists at " + trnsfm)
        logger.debug(antsreg.cmdline)
    return trnsfm


# Faster ants diffeomorphic registration
# takes moving and template niis and a work dir
# performs fast diffeomorphic registration and returns a list of transforms
def fast_diffeo_reg(moving_nii, template_nii, work_dir, option=None, repeat=False):
    # logging
    logger = logging.getLogger("my_logger")
    # get basenames
    moving_name = os.path.basename(moving_nii).split(".")[0]
    template_name = os.path.basename(template_nii).split(".")[0]
    outprefix = os.path.join(work_dir, moving_name + "_2_" + template_name + "_")

    # registration setup
    antsreg = Registration()
    antsreg.inputs.args = '--float'
    antsreg.inputs.fixed_image = template_nii
    antsreg.inputs.moving_image = moving_nii
    antsreg.inputs.output_transform_prefix = outprefix
    antsreg.inputs.num_threads = multiprocessing.cpu_count()
    antsreg.terminal_output = 'none'
    if isinstance(option, dict) and "reg_com" in option.keys():
        antsreg.inputs.initial_moving_transform_com = option["reg_com"]
    else:
        antsreg.inputs.initial_moving_transform_com = 1  # use center of mass for initial transform by default
    antsreg.inputs.winsorize_lower_quantile = 0.005
    antsreg.inputs.winsorize_upper_quantile = 0.995
    antsreg.inputs.shrink_factors = [[6, 4, 2], [4, 2]]
    antsreg.inputs.smoothing_sigmas = [[4, 2, 1], [2, 1]]
    antsreg.inputs.sigma_units = ['mm', 'mm']
    antsreg.inputs.transforms = ['Affine', 'SyN']
    antsreg.inputs.use_histogram_matching = [True, True]
    antsreg.inputs.write_composite_transform = True
    antsreg.inputs.metric = ['Mattes', 'Mattes']
    antsreg.inputs.metric_weight = [1.0, 1.0]
    antsreg.inputs.number_of_iterations = [[1000, 500, 250], [50, 50]]
    antsreg.inputs.convergence_threshold = [1e-05, 1e-05]
    antsreg.inputs.convergence_window_size = [5, 5]
    antsreg.inputs.radius_or_number_of_bins = [32, 32]
    antsreg.inputs.sampling_strategy = ['Regular', 'None']  # 'None'
    antsreg.inputs.sampling_percentage = [0.25, 1]
    antsreg.inputs.transform_parameters = [(0.1,), (0.1, 3.0, 0.0)]

    trnsfm = outprefix + "Composite.h5"
    if not os.path.isfile(trnsfm) or repeat:
        logger.info("- Registering image " + moving_nii + " to " + template_nii)
        logger.debug(antsreg.cmdline)
        antsreg.run()
    else:
        logger.info("- Warp file already exists at " + trnsfm)
        logger.debug(antsreg.cmdline)
    return trnsfm


# ANTS translation
# takes moving and template niis and a work dir
# performs fast translation only registration and returns a list of transforms
def trans_reg(moving_nii, template_nii, work_dir, option=None, repeat=False):
    # logging
    logger = logging.getLogger("my_logger")
    # get basenames
    moving_name = os.path.basename(moving_nii).split(".")[0]
    template_name = os.path.basename(template_nii).split(".")[0]
    outprefix = os.path.join(work_dir, moving_name + "_2_" + template_name + "_")

    # registration setup
    antsreg = Registration()
    antsreg.inputs.args = '--float'
    antsreg.inputs.fixed_image = template_nii
    antsreg.inputs.moving_image = moving_nii
    antsreg.inputs.output_transform_prefix = outprefix
    antsreg.inputs.num_threads = multiprocessing.cpu_count()
    antsreg.inputs.smoothing_sigmas = [[6, 4, 1, 0]]
    antsreg.inputs.sigma_units = ['vox']
    antsreg.inputs.transforms = ['Translation']  # ['Rigid', 'Affine', 'SyN']
    antsreg.terminal_output = 'none'
    antsreg.inputs.use_histogram_matching = True
    antsreg.inputs.write_composite_transform = True
    if isinstance(option, dict) and "reg_com" in option.keys():
        antsreg.inputs.initial_moving_transform_com = option["reg_com"]
    else:
        antsreg.inputs.initial_moving_transform_com = 1  # use center of mass for initial transform by default
    antsreg.inputs.winsorize_lower_quantile = 0.005
    antsreg.inputs.winsorize_upper_quantile = 0.995
    antsreg.inputs.metric = ['Mattes']  # ['MI', 'MI', 'CC']
    antsreg.inputs.metric_weight = [1.0]
    antsreg.inputs.number_of_iterations = [[1000, 500, 250, 50]]  # [100, 70, 50, 20]
    antsreg.inputs.convergence_threshold = [1e-07]
    antsreg.inputs.convergence_window_size = [10]
    antsreg.inputs.radius_or_number_of_bins = [32]  # 4
    antsreg.inputs.sampling_strategy = ['Regular']  # 'None'
    antsreg.inputs.sampling_percentage = [0.25]  # 1
    antsreg.inputs.shrink_factors = [[4, 3, 2, 1]]  # *3
    antsreg.inputs.transform_parameters = [(0.1,)]  # (0.1, 3.0, 0.0) # affine gradient step

    trnsfm = outprefix + "Composite.h5"
    if not os.path.isfile(trnsfm) or repeat:
        logger.info("- Registering image " + moving_nii + " to " + template_nii)
        logger.debug(antsreg.cmdline)
        antsreg.run()
    else:
        logger.info("- Warp file already exists at " + trnsfm)
        logger.debug(antsreg.cmdline)
    return trnsfm


# ANTS translation
# takes moving and template niis and a work dir
# performs fast translation only registration and returns a list of transforms
def rigid_reg(moving_nii, template_nii, work_dir, option=None, repeat=False):
    # logging
    logger = logging.getLogger("my_logger")
    # get basenames
    moving_name = os.path.basename(moving_nii).split(".")[0]
    template_name = os.path.basename(template_nii).split(".")[0]
    outprefix = os.path.join(work_dir, moving_name + "_2_" + template_name + "_")

    # registration setup
    antsreg = Registration()
    antsreg.inputs.args = '--float'
    antsreg.inputs.fixed_image = template_nii
    antsreg.inputs.moving_image = moving_nii
    antsreg.inputs.output_transform_prefix = outprefix
    antsreg.inputs.num_threads = multiprocessing.cpu_count()
    antsreg.inputs.smoothing_sigmas = [[6, 4, 1, 0]]
    antsreg.inputs.sigma_units = ['vox']
    antsreg.inputs.transforms = ['Rigid']  # ['Rigid', 'Affine', 'SyN']
    antsreg.terminal_output = 'none'
    antsreg.inputs.use_histogram_matching = True
    antsreg.inputs.write_composite_transform = True
    if isinstance(option, dict) and "reg_com" in option.keys():
        antsreg.inputs.initial_moving_transform_com = option["reg_com"]
    else:
        antsreg.inputs.initial_moving_transform_com = 1  # use center of mass for initial transform by default
    antsreg.inputs.winsorize_lower_quantile = 0.005
    antsreg.inputs.winsorize_upper_quantile = 0.995
    antsreg.inputs.metric = ['Mattes']  # ['MI', 'MI', 'CC']
    antsreg.inputs.metric_weight = [1.0]
    antsreg.inputs.number_of_iterations = [[1000, 500, 250, 50]]  # [100, 70, 50, 20]
    antsreg.inputs.convergence_threshold = [1e-07]
    antsreg.inputs.convergence_window_size = [10]
    antsreg.inputs.radius_or_number_of_bins = [32]  # 4
    antsreg.inputs.sampling_strategy = ['Regular']  # 'None'
    antsreg.inputs.sampling_percentage = [0.25]  # 1
    antsreg.inputs.shrink_factors = [[4, 3, 2, 1]]  # *3
    antsreg.inputs.transform_parameters = [(0.1,)]  # (0.1, 3.0, 0.0) # affine gradient step

    trnsfm = outprefix + "Composite.h5"
    if not os.path.isfile(trnsfm) or repeat:
        logger.info("- Registering image " + moving_nii + " to " + template_nii)
        logger.debug(antsreg.cmdline)
        antsreg.run()
    else:
        logger.info("- Warp file already exists at " + trnsfm)
        logger.debug(antsreg.cmdline)
    return trnsfm


# Ants apply transforms to list
# takes moving and reference niis, an output filename, plus a transform list
# applys transform and saves output as output_nii
def ants_apply(moving_nii, reference_nii, interp, transform_list, work_dir, invert_bool=False, repeat=False):
    # logging
    logger = logging.getLogger("my_logger")
    # enforce list
    if not isinstance(moving_nii, list):
        moving_nii = [moving_nii]
    if not isinstance(transform_list, list):
        transform_list = [transform_list]
    # create output list of same shape
    output_nii = moving_nii
    # define extension
    ext = ".nii"
    # for loop for applying reg
    for ind, mvng in enumerate(moving_nii, 0):
        # define output path
        output_nii[ind] = os.path.join(work_dir, os.path.basename(mvng).split(ext)[0] + '_w.nii.gz')
        # do registration if not already done
        antsapply = ApplyTransforms()
        antsapply.inputs.dimension = 3
        antsapply.terminal_output = 'none'  # suppress terminal output
        antsapply.inputs.input_image = mvng
        antsapply.inputs.reference_image = reference_nii
        antsapply.inputs.output_image = output_nii[ind]
        antsapply.inputs.interpolation = interp
        antsapply.inputs.default_value = 0
        antsapply.inputs.transforms = transform_list
        antsapply.inputs.invert_transform_flags = [invert_bool] * len(transform_list)
        if not os.path.isfile(output_nii[ind]) or repeat:
            logger.info("- Creating warped image " + output_nii[ind])
            logger.debug(antsapply.cmdline)
            antsapply.run()
        else:
            logger.info("- Transformed image already exists at " + output_nii[ind])
            logger.debug(antsapply.cmdline)
    # if only 1 label, don't return array
    if len(output_nii) == 1:
        output_nii = output_nii[0]
    return output_nii


# register data together using reg_target as target, if its a file, use it
# if not assume its a dict key for an already registered file
# there are multiple loops here because dicts dont preserve order, and we need it for some registration steps
def reg_series(ser_dict, repeat=False):
    # logging
    logger = logging.getLogger("my_logger")
    logger.info("REGISTERING IMAGES:")
    # dcm_dir prep
    dcm_dir = ser_dict["info"]["dcmdir"]
    # sort serdict keys so that the atlas reg comes up first - this makes sure atlas registration is first
    sorted_keys = []
    for key in sorted(ser_dict.keys()):
        if "reg_target" in ser_dict[key] and ser_dict[key]["reg_target"] == "atlas":
            sorted_keys.insert(0, key)
        else:
            sorted_keys.append(key)
    # make sure serdict keys requiring other registrations to be performed first are at end of list
    last = []
    tmp = []
    for key in sorted_keys:
        if "reg_last" in ser_dict[key] and ser_dict[key]["reg_last"]:
            last.append(key)
        else:
            tmp.append(key)
    for key in last:
        tmp.append(key)
    sorted_keys = tmp

    # if reg is false, or if there is no input file found, then just make the reg filename same as unreg filename
    for ser in sorted_keys:
        # first, if there is no filename, set to None
        if "filename" not in ser_dict[ser].keys():
            ser_dict[ser].update({"filename": "None"})
        if ser_dict[ser]["filename"] == "None" or "reg" not in ser_dict[ser].keys() or not ser_dict[ser]["reg"]:
            ser_dict[ser].update({"filename_reg": ser_dict[ser]["filename"]})
            ser_dict[ser].update({"transform": "None"})
            ser_dict[ser].update({"reg": False})
        # if reg True, then do the registration using translation, affine, nonlin, or just applying existing transform
    # handle translation registration
    for ser in sorted_keys:
        if ser_dict[ser]["reg"] == "trans":
            if os.path.isfile(ser_dict[ser]["reg_target"]):
                template = ser_dict[ser]["reg_target"]
            else:
                template = ser_dict[ser_dict[ser]["reg_target"]]["filename_reg"]
            # handle surrogate moving image
            if "reg_moving" in ser_dict[ser]:
                movingr = ser_dict[ser]["reg_moving"]
                movinga = ser_dict[ser]["filename"]
            else:
                movingr = ser_dict[ser]["filename"]
                movinga = ser_dict[ser]["filename"]
            # handle registration options here
            if "reg_option" in ser_dict[ser].keys():
                option = ser_dict[ser]["reg_option"]
            else:
                option = None
            transforms = trans_reg(movingr, template, os.path.dirname(dcm_dir), option, repeat)
            # handle interp option
            if "interp" in ser_dict[ser].keys():
                interp = ser_dict[ser]["interp"]
            else:
                interp = 'Linear'
            niiout = ants_apply(movinga, template, interp, transforms, os.path.dirname(dcm_dir), repeat)
            ser_dict[ser].update({"filename_reg": niiout})
            ser_dict[ser].update({"transform": transforms})
    # handle rigid registration
    for ser in sorted_keys:
        if ser_dict[ser]["reg"] == "rigid":
            if os.path.isfile(ser_dict[ser]["reg_target"]):
                template = ser_dict[ser]["reg_target"]
            else:
                template = ser_dict[ser_dict[ser]["reg_target"]]["filename_reg"]
            # handle surrogate moving image
            if "reg_moving" in ser_dict[ser]:
                movingr = ser_dict[ser]["reg_moving"]
                movinga = ser_dict[ser]["filename"]
            else:
                movingr = ser_dict[ser]["filename"]
                movinga = ser_dict[ser]["filename"]
            # handle registration options here
            if "reg_option" in ser_dict[ser].keys():
                option = ser_dict[ser]["reg_option"]
            else:
                option = None
            transforms = rigid_reg(movingr, template, os.path.dirname(dcm_dir), option, repeat)
            # handle interp option
            if "interp" in ser_dict[ser].keys():
                interp = ser_dict[ser]["interp"]
            else:
                interp = 'Linear'
            niiout = ants_apply(movinga, template, interp, transforms, os.path.dirname(dcm_dir), repeat)
            ser_dict[ser].update({"filename_reg": niiout})
            ser_dict[ser].update({"transform": transforms})
    # handle affine registration
    for ser in sorted_keys:
        if ser_dict[ser]["reg"] == "affine":
            if os.path.isfile(ser_dict[ser]["reg_target"]):
                template = ser_dict[ser]["reg_target"]
            else:
                template = ser_dict[ser_dict[ser]["reg_target"]]["filename_reg"]
            # handle surrogate moving image
            if "reg_moving" in ser_dict[ser]:
                movingr = ser_dict[ser]["reg_moving"]
                movinga = ser_dict[ser]["filename"]
            else:
                movingr = ser_dict[ser]["filename"]
                movinga = ser_dict[ser]["filename"]
            if os.path.isfile(movingr) and os.path.isfile(template):  # make sure template and moving files exist
                # handle registration options here
                if "reg_option" in ser_dict[ser].keys():
                    option = ser_dict[ser]["reg_option"]
                else:
                    option = None
                transforms = affine_reg(movingr, template, os.path.dirname(dcm_dir), option, repeat)
                # handle interp option
                if "interp" in ser_dict[ser].keys():
                    interp = ser_dict[ser]["interp"]
                else:
                    interp = 'Linear'
                niiout = ants_apply(movinga, template, interp, transforms, os.path.dirname(dcm_dir), repeat)
                ser_dict[ser].update({"filename_reg": niiout})
                ser_dict[ser].update({"transform": transforms})
    # handle faster affine registration
    for ser in sorted_keys:
        if ser_dict[ser]["reg"] == "fast_affine":
            if os.path.isfile(ser_dict[ser]["reg_target"]):
                template = ser_dict[ser]["reg_target"]
            else:
                template = ser_dict[ser_dict[ser]["reg_target"]]["filename_reg"]
            # handle surrogate moving image
            if "reg_moving" in ser_dict[ser]:
                movingr = ser_dict[ser]["reg_moving"]
                movinga = ser_dict[ser]["filename"]
            else:
                movingr = ser_dict[ser]["filename"]
                movinga = ser_dict[ser]["filename"]
            if os.path.isfile(movingr) and os.path.isfile(template):  # make sure template and moving files exist
                # handle registration options here
                if "reg_option" in ser_dict[ser].keys():
                    option = ser_dict[ser]["reg_option"]
                else:
                    option = None
                transforms = fast_affine_reg(movingr, template, os.path.dirname(dcm_dir), option, repeat)
                # handle interp option
                if "interp" in ser_dict[ser].keys():
                    interp = ser_dict[ser]["interp"]
                else:
                    interp = 'Linear'
                niiout = ants_apply(movinga, template, interp, transforms, os.path.dirname(dcm_dir), repeat)
                ser_dict[ser].update({"filename_reg": niiout})
                ser_dict[ser].update({"transform": transforms})
    # handle diffeo registration
    for ser in sorted_keys:
        if ser_dict[ser]["reg"] == "diffeo":
            if os.path.isfile(ser_dict[ser]["reg_target"]):
                template = ser_dict[ser]["reg_target"]
            else:
                template = ser_dict[ser_dict[ser]["reg_target"]]["filename_reg"]
            # handle surrogate moving image
            if "reg_moving" in ser_dict[ser]:
                movingr = ser_dict[ser]["reg_moving"]
                movinga = ser_dict[ser]["filename"]
            else:
                movingr = ser_dict[ser]["filename"]
                movinga = ser_dict[ser]["filename"]
            if os.path.isfile(movingr) and os.path.isfile(template):  # check that all files exist prior to reg
                # handle registration options here
                if "reg_option" in ser_dict[ser].keys():
                    option = ser_dict[ser]["reg_option"]
                else:
                    option = None
                transforms = diffeo_reg(movingr, template, os.path.dirname(dcm_dir), option, repeat)
                # handle interp option
                if "interp" in ser_dict[ser].keys():
                    interp = ser_dict[ser]["interp"]
                else:
                    interp = 'Linear'
                niiout = ants_apply(movinga, template, interp, transforms, os.path.dirname(dcm_dir), repeat)
                ser_dict[ser].update({"filename_reg": niiout})
                ser_dict[ser].update({"transform": transforms})
    # handle faster diffeo registration
    for ser in sorted_keys:
        if ser_dict[ser]["reg"] == "fast_diffeo":
            if os.path.isfile(ser_dict[ser]["reg_target"]):
                template = ser_dict[ser]["reg_target"]
            else:
                template = ser_dict[ser_dict[ser]["reg_target"]]["filename_reg"]
            # handle surrogate moving image
            if "reg_moving" in ser_dict[ser]:
                movingr = ser_dict[ser]["reg_moving"]
                movinga = ser_dict[ser]["filename"]
            else:
                movingr = ser_dict[ser]["filename"]
                movinga = ser_dict[ser]["filename"]
            if os.path.isfile(movingr) and os.path.isfile(template):  # check that all files exist prior to reg
                # handle registration options here
                if "reg_option" in ser_dict[ser].keys():
                    option = ser_dict[ser]["reg_option"]
                else:
                    option = None
                transforms = fast_diffeo_reg(movingr, template, os.path.dirname(dcm_dir), option, repeat)
                # handle interp option
                if "interp" in ser_dict[ser].keys():
                    interp = ser_dict[ser]["interp"]
                else:
                    interp = 'Linear'
                niiout = ants_apply(movinga, template, interp, transforms, os.path.dirname(dcm_dir), repeat)
                ser_dict[ser].update({"filename_reg": niiout})
                ser_dict[ser].update({"transform": transforms})
    # handle applying an existing transform (assumes reg entry is the key for another series' transform)
    for ser in sorted_keys:
        if ser_dict[ser]["reg"] in sorted_keys:
            try:
                transforms = ser_dict[ser_dict[ser]["reg"]]["transform"]
                template = ser_dict[ser_dict[ser]["reg"]]["filename_reg"]
                moving = ser_dict[ser]["filename"]
                # handle interp option
                if "interp" in ser_dict[ser].keys():
                    interp = ser_dict[ser]["interp"]
                else:
                    interp = 'Linear'
                niiout = ants_apply(moving, template, interp, transforms, os.path.dirname(dcm_dir), repeat)
                ser_dict[ser].update({"filename_reg": niiout})
                ser_dict[ser].update({"transform": transforms})
            except Exception:
                logger.info("- Error attempting to apply existing transform to seies {}".format(ser))
    return ser_dict


# split asl and anatomic image (if necessary)
def split_asl(ser_dict):
    # logging
    logger = logging.getLogger("my_logger")
    logger.info("- Splitting ASL using split_asl function")
    # define files
    aslperf = ser_dict["ASL"]["filename"]
    aslperfa = aslperf.rsplit(".nii", 1)[0] + "a.nii.gz"
    anat_outname = aslperf.rsplit(".nii", 1)[0] + "_anat.nii.gz"
    # anat_json = aslperf.rsplit(".nii", 1)[0] + "_anat.json"
    # handle when dcm2niix converts to two different files
    if os.path.isfile(aslperfa):
        perfnii = nib.load(aslperf)
        perfanii = nib.load(aslperfa)
        if np.mean(perfnii.get_fdata()) > np.mean(perfanii.get_fdata()):
            os.rename(aslperf, anat_outname)
            os.rename(aslperfa, aslperf)
        else:
            os.rename(aslperfa, anat_outname)
    # handle original case where asl perfusion is a 4D image with anat and perfusion combined
    if os.path.isfile(aslperf):
        if not os.path.isfile(anat_outname):
            nii = nib.load(aslperf)
            if len(nii.shape) > 3:
                img = nii.get_fdata()
                aslimg = np.squeeze(img[:, :, :, 0])
                anatimg = np.squeeze(img[:, :, :, 1])
                aff = nii.affine
                aslniiout = nib.Nifti1Image(aslimg, aff)
                asl_outname = aslperf
                anatniiout = nib.Nifti1Image(anatimg, aff)
                nib.save(aslniiout, asl_outname)
                nib.save(anatniiout, anat_outname)
        # add new dict entry to use anatomy image for registration
        ser_dict["ASL"].update({"reg_moving": anat_outname})
    else:
        ser_dict.update({"ASL": {"filename": "None", "reg": False}})
    return ser_dict


# splitter for handling multi-direction dwi data
def split_dwi(ser_dict):
    # logging
    logger = logging.getLogger("my_logger")
    # define filenames
    dwi = ser_dict["DWI"]["filename"]
    dwi_bvals = dwi.rsplit(".nii", 1)[0] + ".bval"
    dwi_bvecs = dwi.rsplit(".nii", 1)[0] + ".bvec"
    b0 = dwi.rsplit("DWI.nii", 1)[0] + "B0.nii.gz"
    # load dwi
    nii = nib.load(dwi)
    img = nii.get_fdata()
    # if 4D
    if len(img.shape) == 4:
        # if bvals file is present, use that to split B0 and DWI
        if os.path.isfile(dwi_bvals):
            logger.info("- Splitting DWI/B0 based on bvals file")
            # get bvals
            with open(dwi_bvals, 'r') as f:
                reader = csv.reader(f, delimiter='\t')
                bvals = list(reader)[0]
                bvals = np.array([float(item) for item in bvals])

            # get average b0 and average bmax
            b0_inds = np.nonzero(bvals == 0)[0]
            b0_img = np.squeeze(np.mean(img[:, :, :, b0_inds], -1))
            dwi_max_inds = bvals == np.max(bvals)
            dwi_img = np.squeeze(np.mean(img[:, :, :, dwi_max_inds], -1))
        else:
            logger.info("- Splitting DWI/B0 based on average image intensity")
            # b0 is the image with highest average value
            max_ind = int(np.argmax(np.mean(img, (0, 1, 2))))
            b0_img = img[:, :, :, max_ind]
            # DWI is first image that is not b0
            nonbo_inds = [0, 1, 2, 3]
            nonbo_inds.remove(max_ind)
            dwi_img = img[:, :, :, nonbo_inds[0]]
        # save
        dwi_nii = nib.Nifti1Image(dwi_img, nii.affine, nii.header)
        bo_nii = nib.Nifti1Image(b0_img, nii.affine, nii.header)
        nib.save(dwi_nii, dwi)
        nib.save(bo_nii, b0)
        ser_dict.update({"B0": {"filename": b0,
                                "reg": "fast_affine",
                                "reg_last": True,
                                "reg_target": "T2FS",
                                "reg_option": {"reg_com": 0},
                                "bias": False}})
    # if not 4D, then dont do anything
    else:
        logger.info("- DWI is not 4D, so splitting was not performed")

    # regardless of what was done, delete bvals and vecs if present
    for f in [dwi_bvals, dwi_bvecs]:
        if os.path.isfile(f):
            os.remove(f)

    return ser_dict


# combine a split 55 direction DTI file if necessary
def combine_dti55(ser_dict):
    # logging
    logger = logging.getLogger("my_logger")
    # define file names
    dti = ser_dict["DTI"]["filename"]
    dtia = dti.rsplit(".nii", 1)[0] + "a.nii.gz"
    dti_bvals = dti.rsplit(".nii", 1)[0] + ".bval"
    dti_bvecs = dti.rsplit(".nii", 1)[0] + ".bvec"
    dtia_bvals = dti.rsplit(".nii", 1)[0] + "a.bval"
    dtia_bvecs = dti.rsplit(".nii", 1)[0] + "a.bvec"
    # first check if DTI bvals is present, if not, nothing can be done
    if not os.path.isfile(dti_bvals):
        return ser_dict
    # next check how many bvals in original DTI and return if exactly 56 (55 directions + 1 B0)
    with open(dti_bvals, 'r') as f:
        reader = csv.reader(f, delimiter='\t')
        rows = [item for item in reader]
    if len(rows[0]) == 56:
        return ser_dict
    # handle case where directions > 56
    logger.info("- Averaging multiple B0s in DTI data")
    if len(rows[0]) > 56:
        # assume multiple b0s and average them
        num_b0 = len(rows[0]) - 55
        dti_f = nib.load(dti)
        dti_d = dti_f.get_fdata()
        b0s = np.mean(dti_d[:, :, :, 0:num_b0], axis=3)
        dti_d = np.concatenate([np.expand_dims(b0s, axis=3), dti_d[:, :, :, num_b0:]], axis=3)
        dti_f = nib.Nifti1Image(dti_d, dti_f.affine, dti_f.header)
        nib.save(dti_f, dti)
        # remove extra zeros from bvals and vecs
        # read original vals
        with open(dti_bvals, 'r') as f:
            reader = csv.reader(f, delimiter='\t')
            rows = [item[num_b0 - 1:] for item in reader]
        with open(dti_bvals, 'w+') as f:
            writer = csv.writer(f, delimiter='\t')
            writer.writerows(rows)
        # read original vecs
        if os.path.isfile(dtia_bvecs):
            with open(dti_bvecs, 'r') as f:
                reader = csv.reader(f, delimiter='\t')
                rows = [item[num_b0 - 1:] for item in reader]
            with open(dti_bvecs, 'w+') as f:
                writer = csv.writer(f, delimiter='\t')
                writer.writerows(rows)
        return ser_dict
    # now check if DTIa exists, if not, then return
    if not os.path.isfile(dtia):
        return ser_dict
    # DTIa exists and DTI has less than 56 volumes, combine DTI and DTIa
    # first make sure bvals files are present
    if not os.path.isfile(dtia_bvals):
        return ser_dict
    logger.info("- Combining split DTI file using combine_dti55 function")
    dti_f = nib.load(dti)
    dti_d = dti_f.get_fdata()
    dtia_f = nib.load(dtia)
    dtia_d = dtia_f.get_fdata()
    dti_d = np.concatenate([dti_d, np.reshape(dtia_d, [dtia_d.shape[0], dtia_d.shape[1], dtia_d.shape[2], -1])], axis=3)
    dti_f = nib.Nifti1Image(dti_d, dti_f.affine, dti_f.header)
    nib.save(dti_f, dti)
    # combine bvals
    # read original vals
    with open(dti_bvals, 'r') as f:
        reader = csv.reader(f, delimiter='\t')
        rows = [item for item in reader]
    # read a vals
    with open(dtia_bvals, 'r') as f:
        reader = csv.reader(f, delimiter='\t')
        rowsa = [item for item in reader]
    # combine vals
    outrows = [row + rowa for row, rowa in zip(rows, rowsa)]
    with open(dti_bvals, 'w+') as outfile:
        writer = csv.writer(outfile, delimiter='\t')
        writer.writerows(outrows)
    # combine bvecs if both of the necessary files exist
    if all([os.path.isfile(f) for f in [dti_bvecs, dtia_bvecs]]):
        # read original vecs
        with open(dti_bvecs, 'r') as f:
            reader = csv.reader(f, delimiter='\t')
            rows = [item for item in reader]
        # read a vecs
        with open(dtia_bvecs, 'r') as f:
            reader = csv.reader(f, delimiter='\t')
            rowsa = [item for item in reader]
        # combine vecs
        outrows = [row + rowa for row, rowa in zip(rows, rowsa)]
        with open(dti_bvecs, 'w+') as outfile:
            writer = csv.writer(outfile, delimiter='\t')
            writer.writerows(outrows)
    # remove a_vecs and a_vals files if they exist
    delete = [dtia, dtia_bvals, dtia_bvecs]
    for item in delete:
        if os.path.isfile(item):
            os.remove(item)


# function for converting dcm2nii bval and bvecs files to the necessary inputs for FSL eddy
# returns a list of filenames [bvals, bvecs, acqp, index] or None if files cannot be made
def bvec_convert(bvals, bvecs):
    # logging
    logger = logging.getLogger("my_logger")
    # get base directory define outputs and check that files exist
    if not any([os.path.isfile(f) for f in [bvals, bvecs]]):
        return None
    path_stem = bvals.rsplit('.bval', 1)[0]
    acqp = path_stem + '_acqp.txt'
    index = path_stem + '_index.txt'
    # if all the diseried outputs already exist, then return their paths
    if all([os.path.isfile(f) for f in [bvals, bvecs, acqp, index]]):
        return [bvals, bvecs, acqp, index]
    # here bvals and bvecs exist but other files are missing, so we do conversion
    # bvecs first - read from file as tab delimited (dcm2nii output format), write as space delimited (FSL format)
    logger.info("- Generating param files for FSL eddy using bval and bvec files from dicom conversion")
    with open(bvecs, 'r') as f:
        reader = csv.reader(f, delimiter='\t')
        bvecs_rows = [row for row in reader]
    # check if bvecs was actually space delimited, if so don't do anything. If not, then overwrite as space delimited.
    if len(bvecs_rows[0]) > 1:
        # check for mutliple B0 entries here - not sure if this is a real issue.

        with open(bvecs, 'w+') as f:
            writer = csv.writer(f, delimiter=' ', quoting=csv.QUOTE_MINIMAL)
            writer.writerows(bvecs_rows)
    # bvals next - read from file as tab delimited (dcm2nii output format), write as space delimited (FSL format)
    with open(bvals, 'r') as f:
        reader = csv.reader(f, delimiter='\t')
        bvals_rows = [row for row in reader]
    if len(bvals_rows[0]) > 1:
        with open(bvals, 'w+') as f:
            writer = csv.writer(f, delimiter=' ', quoting=csv.QUOTE_MINIMAL)
            writer.writerows(bvals_rows)
    # make acqp and index files
    # acqp is hard coded for 4th param
    # https://fsl.fmrib.ox.ac.uk/fsl/fslwiki/eddy/Faq#How_do_I_know_what_to_put_into_my_--acqp_file
    # he fourth element in each row is the time (in seconds) between reading the center of the first echo and reading
    # the center of the last echo. It is the "dwell time" multiplied by "number of PE steps - 1" and it is also the
    # reciprocal of the PE bandwidth/pixel.
    acqp_list = [[0, 1, 0, 0.0655]]  # hard-coded for now.
    with open(acqp, 'w+') as f:
        writer = csv.writer(f, delimiter=' ', quoting=csv.QUOTE_MINIMAL)
        writer.writerows(acqp_list)
    # index length here is determined by number of bvals, also checks if the read bvals was actually space delimited
    index_len = len(bvals_rows[0]) if len(bvals_rows[0]) > 1 else len(bvals_rows[0][0].split(' '))
    index_list = [[1] * index_len]
    with open(index, 'w+') as f:
        writer = csv.writer(f, delimiter=' ', quoting=csv.QUOTE_MINIMAL)
        writer.writerows(index_list)
    # return outputs as a list
    return [bvals, bvecs, acqp, index]


# DTI processing if present
def dti_proc(ser_dict, dti_index, dti_acqp, dti_bvec, dti_bval, repeat=False):
    # logging
    logger = logging.getLogger("my_logger")
    # id setup
    idno = ser_dict["info"]["id"]
    # dcm_dir prep
    dcm_dir = ser_dict["info"]["dcmdir"]
    if "filename" in ser_dict["DTI"].keys() and os.path.isfile(ser_dict["DTI"]["filename"]):
        # define DTI input
        dti_in = ser_dict["DTI"]["filename"]
        logger.info("PROCESSING DTI")
        logger.info("- DTI file found at " + dti_in)
        # separate b0 image if not already done
        b0 = os.path.join(os.path.dirname(dcm_dir), idno + "_DTI_b0.nii.gz")
        fslroi = ExtractROI(in_file=dti_in, roi_file=b0, t_min=0, t_size=1)
        fslroi.terminal_output = "none"
        if not os.path.isfile(b0):
            logger.info("- Separating b0 image from DTI")
            logger.debug(fslroi.cmdline)
            fslroi.run()
        else:
            logger.info("- B0 image already exists at " + b0)
            logger.debug(fslroi.cmdline)
        # add b0 to list for affine registration
        ser_dict.update({"DTI_b0": {"filename": b0, "reg": "diffeo", "reg_target": "FLAIR", "no_norm": True}})
        # make BET mask
        dti_mask = os.path.join(os.path.dirname(dcm_dir), idno + "_DTI_mask.nii.gz")
        btr = BET()
        btr.inputs.in_file = b0  # base mask on b0 image
        btr.inputs.out_file = os.path.join(os.path.dirname(dcm_dir), idno + "_DTI")  # output prefix _mask is autoadded
        btr.inputs.mask = True
        btr.inputs.no_output = True
        btr.inputs.frac = 0.2  # lower threshold for more inclusive mask
        if not os.path.isfile(dti_mask) or repeat:
            logger.info("- Making BET brain mask for DTI")
            logger.debug(btr.cmdline)
            _ = btr.run()
        else:
            logger.info("- DTI BET masking already exists at " + dti_mask)
            logger.debug(btr.cmdline)
        # if bvces/bvals files exist then use these as params, else use defaults
        bvals = os.path.join(os.path.dirname(dcm_dir), idno + "_DTI.bval")
        bvecs = os.path.join(os.path.dirname(dcm_dir), idno + "_DTI.bvec")
        acqp = os.path.join(os.path.dirname(dcm_dir), idno + "_DTI_acqp.txt")
        index = os.path.join(os.path.dirname(dcm_dir), idno + "_DTI_index.txt")
        dti_params = bvec_convert(bvals, bvecs)
        # if result was not None, set params to new values, if it was None, then use defaults
        if dti_params:
            my_dti_bval, my_dti_bvec, my_dti_acqp, my_dti_index = dti_params
        else:
            if not all([os.path.isfile(f) for f in [bvals, bvecs, acqp, index]]):
                # copy deafult DTI param files to data directory if the dont exist
                shutil.copy2(dti_bval, bvals)
                shutil.copy2(dti_bvec, bvecs)
                shutil.copy2(dti_acqp, acqp)
                shutil.copy2(dti_index, index)
            # set processing params to local file names
            my_dti_bval, my_dti_bvec, my_dti_acqp, my_dti_index = [bvals, bvecs, acqp, index]
        # do eddy correction with outlier replacement
        dti_out = os.path.join(os.path.dirname(dcm_dir), idno + "_DTI_eddy")
        dti_outfile = os.path.join(os.path.dirname(dcm_dir), idno + "_DTI_eddy.nii.gz")
        eddy = Eddy()
        eddy.inputs.in_file = dti_in
        eddy.inputs.out_base = dti_out
        eddy.inputs.in_mask = dti_mask
        eddy.inputs.in_index = my_dti_index
        eddy.inputs.in_acqp = my_dti_acqp
        eddy.inputs.in_bvec = my_dti_bvec
        eddy.inputs.in_bval = my_dti_bval
        eddy.inputs.use_cuda = True
        eddy.inputs.repol = True
        eddy.terminal_output = "none"
        stderr = 'None'
        if not os.path.isfile(dti_outfile) or repeat:
            logger.info("- Eddy correcting DWIs")
            try:
                logger.debug(eddy.cmdline)
                result = eddy.run()
                stderr = result.runtime.stderr
            except Exception:
                logger.info("- DTI eddy correction failed. Standard error sent to debug logger.")
                logger.debug(stderr)
        else:
            logger.info("- Eddy corrected DWIs already exist at " + dti_outfile)
            logger.debug(eddy.cmdline)
        # do DTI processing only if eddy corrected DTI exists
        if os.path.isfile(dti_outfile):
            fa_out = dti_out + "_FA.nii.gz"
            dti = DTIFit()
            dti.inputs.dwi = dti_outfile
            dti.inputs.bvecs = dti_out + ".eddy_rotated_bvecs"
            dti.inputs.bvals = my_dti_bval
            dti.inputs.base_name = dti_out
            dti.inputs.mask = dti_mask
            # dti.inputs.args = "-w"  # terminating with uncaught exception of type NEWMAT::SingularException
            dti.terminal_output = "none"
            dti.inputs.save_tensor = True
            # dti.ignore_exception = True  # for some reason running though nipype causes error at end
            if os.path.isfile(dti_outfile) and not os.path.isfile(fa_out) or os.path.isfile(dti_outfile) and repeat:
                logger.info("- Fitting DTI")
                try:
                    logger.debug(dti.cmdline)
                    _ = dti.run()
                    # if DTI processing fails to create FA, it may be due to least squares option
                    if not os.path.isfile(fa_out):
                        dti.inputs.args = ""
                        logger.debug(dti.cmdline)
                        _ = dti.run()
                except Exception:
                    if not os.path.isfile(fa_out):
                        logger.info("- Could not process DTI")
                    else:
                        logger.info("- DTI processing completed")
            else:
                if os.path.isfile(fa_out):
                    logger.info("- DTI outputs already exist with prefix " + dti_out)
                    logger.debug(dti.cmdline)
            # if DTI processing completed, add DTI to series_dict for registration (note, DTI is masked at this point)
            if os.path.isfile(fa_out):
                ser_dict.update({"DTI_FA": {"filename": dti_out + "_FA.nii.gz", "reg": "DTI_b0"}})
                ser_dict.update({"DTI_MD": {"filename": dti_out + "_MD.nii.gz", "reg": "DTI_b0", "no_norm": True}})
                ser_dict.update({"DTI_L1": {"filename": dti_out + "_L1.nii.gz", "reg": "DTI_b0", "no_norm": True}})
                ser_dict.update({"DTI_L2": {"filename": dti_out + "_L2.nii.gz", "reg": "DTI_b0", "no_norm": True}})
                ser_dict.update({"DTI_L3": {"filename": dti_out + "_L3.nii.gz", "reg": "DTI_b0", "no_norm": True}})
        else:
            logger.info("- Skipping DTI processing since eddy corrected DTI does not exist")
    return ser_dict


# make mask based on t1gad and flair and apply to all other images
def breast_mask(ser_dict, force_cpu=True, repeat=False):
    # logging
    logger = logging.getLogger("my_logger")
    # id setup
    idno = ser_dict["info"]["id"]
    # dcm_dir prep
    dcm_dir = ser_dict["info"]["dcmdir"]
    # CNN breast masking
    logger.info("BREAST MASKING:")

    # check that necessary files exist
    t1w = os.path.join(os.path.dirname(dcm_dir), idno + "_T1_w.nii.gz")
    if not os.path.isfile(t1w):
        logger.warning("Did not find required image for breast masking: {}".format(t1w))
        return ser_dict
    
    # perform CNN-based brain masking
    param_files = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'breast_mask/mask.json')
    mask_file = os.path.join(os.path.dirname(dcm_dir), idno + "_breast_mask.nii.gz")
    if os.path.isfile(mask_file):
        logger.info("- Breast mask already exists at " + mask_file)
    else:

        logger.info("- creating CNN-based breast mask at " + mask_file)
        if force_cpu:
            logging.info("Forcing CPU (GPU disabled)")
            os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
        maskout = batch_mask(os.path.dirname(dcm_dir), param_files, os.path.dirname(dcm_dir), 'breast_mask',
                             overwrite=repeat, thresh=0.5)
        if maskout:
            logger.info("- Created combined brain mask at " + mask_file)
        else:
            logger.info("- Error creating combined brain mask at " + mask_file)

    # now apply to all other images if mask exists
    if os.path.isfile(mask_file):
        for sers in ser_dict:
            if "filename_reg" in ser_dict[sers]:  # check that filename_reg entry exists
                ser_masked_stem = ser_dict[sers]["filename_reg"].rsplit(".nii", 1)[0]
                if ser_masked_stem.endswith('_w'):
                    ser_masked = ser_masked_stem + "m.nii.gz"
                else:
                    ser_masked = ser_masked_stem + "_wm.nii.gz"

                if repeat or (not os.path.isfile(ser_masked) and os.path.isfile(ser_dict[sers]["filename_reg"])):
                    # apply mask using fsl maths
                    logger.info("- Masking " + ser_dict[sers]["filename_reg"])
                    # prep command line regardless of whether or not work will be done
                    mask_cmd = ApplyMask()
                    mask_cmd.inputs.in_file = ser_dict[sers]["filename_reg"]
                    mask_cmd.inputs.mask_file = mask_file
                    mask_cmd.inputs.out_file = ser_masked
                    mask_cmd.terminal_output = "none"
                    logger.debug(mask_cmd.cmdline)
                    _ = mask_cmd.run()
                    ser_dict[sers].update({"filename_masked": ser_masked})
                elif os.path.isfile(ser_masked):
                    logger.info("- Masked file already exists for " + sers + " at " + ser_masked)
                    # prep command line regardless of whether or not work will be done
                    mask_cmd = ApplyMask()
                    mask_cmd.inputs.in_file = ser_dict[sers]["filename_reg"]
                    mask_cmd.inputs.mask_file = mask_file
                    mask_cmd.inputs.out_file = ser_masked
                    mask_cmd.terminal_output = "none"
                    logger.debug(mask_cmd.cmdline)
                    ser_dict[sers].update({"filename_masked": ser_masked})
                elif sers == "info":
                    pass
                else:
                    logger.info("- Skipping masking for " + sers + " as file does not exist")
            else:
                logger.info("- No filename_reg entry exists for series " + sers)
    else:
        logger.info("- Combined mask file not found, expected location is: " + mask_file)
    return ser_dict


# bias correction
# takes series dict, returns series dict, performs intensity windsorization and n4 bias correction on select volumes
def bias_correct(ser_dict, repeat=False):
    # logging
    logger = logging.getLogger("my_logger")
    logger.info("BIAS CORRECTING IMAGES:")
    # id setup
    idno = ser_dict["info"]["id"]
    # dcm_dir prep
    dcm_dir = ser_dict["info"]["dcmdir"]
    # identify files to bias correct
    for srs in ser_dict:
        if "bias" in ser_dict[srs] and ser_dict[srs]["bias"]:  # only do bias if specified in series dict
            # get name of masked file and the mask itself
            f = os.path.join(os.path.dirname(dcm_dir), idno + "_" + srs + "_wm.nii.gz")
            mask = os.path.join(os.path.dirname(dcm_dir), idno + "_combined_brain_mask.nii.gz")
            if not os.path.isfile(f):
                logger.info("- Skipping bias correction for " + srs + " as masked file does not exist.")
            elif not os.path.isfile(mask):
                logger.info("- Skipping bias correction for " + srs + " as mask image does not exist.")
            else:
                # generate output filenames for corrected image and bias field
                biasfile = str(f.rsplit(".nii", 1)[0]) + "_biasmap.nii.gz"
                truncated_img = str(f.rsplit(".nii", 1)[0]) + "t.nii.gz"
                biasimg = str(truncated_img.rsplit(".nii", 1)[0]) + "b.nii.gz"
                # first truncate image intensities, this also removes any negative values
                if not os.path.isfile(truncated_img) or repeat:
                    logger.info("- Truncating image intensities for " + f)
                    thresh = [0.001, 0.999]  # define thresholds
                    mask_img = nib.load(mask)  # load data
                    mask_img = mask_img.get_fdata()
                    nii = nib.load(f)
                    img = nii.get_fdata()
                    affine = nii.affine
                    vals = np.sort(img[mask_img > 0.], None)  # sort data in order
                    thresh_lo = vals[int(np.round(thresh[0] * len(vals)))]  # define hi and low thresholds
                    thresh_hi = vals[int(np.round(thresh[1] * len(vals)))]
                    img_trunc = np.where(img < thresh_lo, thresh_lo, img)  # truncate low
                    img_trunc = np.where(img_trunc > thresh_hi, thresh_hi, img_trunc)  # truncate hi
                    img_trunc = np.where(mask_img > 0., img_trunc, 0.)  # remask data
                    nii = nib.Nifti1Image(img_trunc, affine)  # make nii and save
                    nib.save(nii, str(truncated_img))
                else:
                    logger.info("- Truncated image already exists at " + truncated_img)
                # run bias correction on truncated image
                # apply N4 bias correction
                n4_cmd = N4BiasFieldCorrection()
                n4_cmd.inputs.copy_header = True
                n4_cmd.inputs.input_image = truncated_img
                n4_cmd.inputs.save_bias = True
                n4_cmd.inputs.bias_image = biasfile
                n4_cmd.inputs.bspline_fitting_distance = 300
                n4_cmd.inputs.bspline_order = 3
                n4_cmd.inputs.convergence_threshold = 1e-6
                n4_cmd.inputs.dimension = 3
                # n4_cmd.inputs.environ=
                n4_cmd.inputs.mask_image = mask
                n4_cmd.inputs.n_iterations = [50, 50, 50, 50]
                n4_cmd.inputs.num_threads = multiprocessing.cpu_count()
                n4_cmd.inputs.output_image = biasimg
                n4_cmd.inputs.shrink_factor = 3
                # n4_cmd.inputs.weight_image=
                if not os.path.isfile(biasimg) or repeat:
                    logger.info("- Bias correcting " + truncated_img)
                    logger.debug(n4_cmd.cmdline)
                    _ = n4_cmd.run()
                    ser_dict[srs].update({"filename_bias": biasimg})
                else:
                    logger.info("- Bias corrected image already exists at " + biasimg)
                    logger.debug(n4_cmd.cmdline)
                    ser_dict[srs].update({"filename_bias": biasimg})
    return ser_dict


# normalize nifits
# takes a series dict and returns a series dict, normalizes all masked niis
# currently uses zero mean and unit variance
def norm_niis(ser_dict, repeat=False):
    # logging
    logger = logging.getLogger("my_logger")
    logger.info("NORMALIZING NIIs:")
    # get list of filenames and normalize them
    for srs in ser_dict:
        if "bias" in ser_dict[srs] and ser_dict[srs]["bias"]:  # do normalization if bias was done
            fn = ser_dict[srs]["filename_bias"]
            normname = os.path.join(os.path.dirname(fn), os.path.basename(fn).split(".")[0] + "n.nii.gz")
            # if normalized file doesn't exist, make it
            if not os.path.isfile(normname) or repeat:
                logger.info("- Normalizing " + srs + " at " + fn)
                # load image into memory
                nii = nib.load(fn)
                affine = nii.affine
                img = nii.get_fdata()
                nzi = np.nonzero(img)
                mean = np.mean(img[nzi])
                std = np.std(img[nzi])
                img = np.where(img != 0., (img - mean) / std, 0.)  # zero mean unit variance for nonzero indices
                nii = nib.Nifti1Image(img, affine)
                nib.save(nii, normname)
                ser_dict[srs].update({"filename_norm": normname})
            else:
                logger.info("- Normalized " + srs + " already exists at " + normname)
                ser_dict[srs].update({"filename_norm": normname})
    return ser_dict


# make 4d nii
# takes series dict, returns series dict, makes a 4d nii using all normed series
def make_nii4d(ser_dict, repeat=False):
    # logging
    logger = logging.getLogger("my_logger")
    logger.info("MAKING 4D NII:")
    # id setup
    idno = ser_dict["info"]["id"]
    # define vars
    files = []
    # get all normalized images and collect them in a list
    for srs in ser_dict:
        if "filename_norm" in ser_dict[srs]:  # normalized files only, others ignored
            files.append(ser_dict[srs]["filename_norm"])
            logger.info("- Adding " + srs + " to nii4D list at " + ser_dict[srs]["filename_norm"])
    if files and len(files) > 1:  # only attempt work if normalized files exist and there is more than 1
        # get dirname from first normalized image, make nii4d name from this
        bdir = os.path.dirname(files[0])
        nii4d = os.path.join(bdir, idno + "_nii4d.nii.gz")
        # if nii4d doesn't exist, make it
        # nii4dcmd = "ImageMath 4 " + nii4d + " TimeSeriesAssemble 1 1 " + " ".join(files)
        # os.system(nii4dcmd)
        merger = Merge()
        merger.inputs.in_files = files
        merger.inputs.dimension = "t"
        merger.inputs.merged_file = nii4d
        merger.terminal_output = "none"
        if not os.path.isfile(nii4d) or repeat:
            logger.info("- Creating 4D nii at " + nii4d)
            logger.debug(merger.cmdline)
            merger.run()
        else:
            logger.info("- 4D Nii already exists at " + nii4d)
            logger.debug(merger.cmdline)
    else:
        logger.info("- Not enough files to make 4D Nii")
    return ser_dict


# print and save series dict
def print_series_dict(series_dict, repeat=False):
    dcm_dir = series_dict["info"]["dcmdir"]
    # first save as a numpy file
    serdict_outfile = os.path.join(os.path.dirname(dcm_dir), series_dict["info"]["id"] + "_metadata.npy")
    if not os.path.isfile(serdict_outfile) or repeat:
        np.save(serdict_outfile, series_dict)
    # save human readable serdict file with binary data removed
    hr_serdict_outfile = os.path.join(os.path.dirname(dcm_dir), series_dict["info"]["id"] + "_metadata_HR.txt")
    if not os.path.isfile(hr_serdict_outfile) or repeat:
        # remove binary entries from dict, also will only print the first dicom path from the list
        def remove_nonstr_from_dict(a_dict):
            new_dict = {}
            for k, v in a_dict.items():
                if isinstance(v, dict):
                    v = remove_nonstr_from_dict(v)
                if isinstance(v, (int, float, complex, str, list, dict)):
                    if k == "dicoms" and isinstance(v, list) and v:  # ensure dicoms is a list and is not empty
                        new_dict[k] = v[0]
                    else:
                        new_dict[k] = v
            return new_dict

        hr_serdict = remove_nonstr_from_dict(series_dict)
        with open(hr_serdict_outfile, 'w') as f:
            f.write("%s" % yaml.safe_dump(hr_serdict))
    return series_dict
