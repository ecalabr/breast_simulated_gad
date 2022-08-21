""" Make ADC from DWI data """

import os
from glob import glob
import argparse
import nibabel as nib
import csv
import numpy as np
from nipype.interfaces.dcm2nii import Dcm2niix


# make adc from dwi data
def dwi2adc(direcs, repeat=False, temp=False):
    # placeholders
    converted = []
    outfiles = []

    # get number of direcs and announce
    n_total = len(direcs)
    print("Performing ADC calculation for a total of " + str(n_total) + " directories")

    # run ITK-snap on each one
    for ind, direc in enumerate(direcs, 1):

        # report
        print("Working on directory {}".format(direc))

        # make sure output doesn't already exist
        if direc.endswith('/'):
            direc = direc[:-1]
        adc_file = os.path.join(direc, os.path.basename(direc) + "_ADC.nii.gz")
        if os.path.isfile(adc_file) and not repeat:
            print("- ADC already exists at {}, work will not be repeated".format(adc_file))
            continue

        # find and load metadata file
        metadata_file = glob(direc + "/*metadata.npy")
        if metadata_file and os.path.isfile(metadata_file[0]):
            print("- Loading metadata file")
            serdict = np.load(metadata_file[0], allow_pickle=True).item()
            assert isinstance(serdict, dict), "Could not retrieve dictionary from metadata file"
        else:
            continue

        # check for DWI file
        if "DWI" in serdict.keys() and "dicoms" in serdict["DWI"].keys() and serdict["DWI"][
            "dicoms"] and os.path.isfile(serdict["DWI"]["dicoms"][0]):

            # convert dwi
            # basic converter initiation
            converter = Dcm2niix()
            converter.inputs.bids_format = False
            converter.inputs.single_file = True
            converter.inputs.args = '-w 2'
            converter.inputs.compress = "y"
            converter.inputs.output_dir = direc
            converter.terminal_output = "allatonce"
            converter.anonymize = True
            converter.inputs.source_names = serdict["DWI"]["dicoms"]
            converter.inputs.out_filename = "DWI_temp"

            # define output file names
            dwi_file = os.path.join(direc, "DWI_temp.nii.gz")
            bvecs_file = os.path.join(direc, "DWI_temp.bvec")
            bvals_file = os.path.join(direc, "DWI_temp.bval")

            # convert if not existing already
            if not all([os.path.isfile(f) for f in [dwi_file, bvecs_file, bvals_file]]):
                print("- Converting DWI")
                result = converter.run()

                # make sure that file wasnt named something else during conversion
                outfiles = result.outputs.converted_files
                if not isinstance(outfiles, list):
                    outfiles = [outfiles]

            # make sure required files exist for processing
            if all([os.path.isfile(f) for f in [dwi_file, bvecs_file, bvals_file]]):

                # load dwi data
                dwi_nii = nib.load(dwi_file)
                img = dwi_nii.get_fdata()

                # if 4D, fit vals to exponential
                if len(img.shape) == 4:

                    # get bvals
                    if os.path.isfile(bvals_file):
                        with open(bvals_file, 'r') as f:
                            reader = csv.reader(f, delimiter='\t')
                            bvals = list(reader)[0]
                            bvals = np.array([float(item) for item in bvals])
                    else:
                        print("- Bvals file not found, so ADC calculation could not be performed")
                        continue

                    # get average b0, average bmax, and their quotient (S/S0)
                    dwi_inds = np.nonzero(bvals)[0]
                    dwi_max_inds = bvals == np.max(bvals)
                    b0_inds = np.nonzero(bvals == 0)[0]
                    b0_img = np.squeeze(np.mean(img[:, :, :, b0_inds], -1))
                    dwi_max_img = np.squeeze(np.mean(img[:, :, :, dwi_max_inds], -1))
                    ss0 = dwi_max_img / b0_img

                    # calculate ADC
                    adc_img = (np.log(ss0) / -np.max(bvals)) * 1000000.
                    adc_img = np.nan_to_num(adc_img)

                    # save
                    adc_nii = nib.Nifti1Image(adc_img.astype(np.float32), dwi_nii.affine)
                    nib.save(adc_nii, adc_file)
                    print("- Saved output to {}".format(adc_file))
                    converted.append(adc_nii)

                # if not 4D, then dont do anything
                else:
                    print("- DWI is not 4D, so ADC calculation could not be performed")

                # clean up temp files
                if not temp:
                    for f in outfiles + [bvecs_file, bvals_file]:
                        if os.path.isfile(f):
                            print("- Removing temporary file {}".format(f))
                            os.remove(f)
        else:
            print("- Missing some required files - ADC calculation not performed")

    return converted


# executed  as script
if __name__ == '__main__':

    # parse input arguments
    parser = argparse.ArgumentParser()
    parser.add_argument('-d', '--data_dir', default=None,
                        help="Path to data directory")
    parser.add_argument('-s', '--start', default=0,
                        help="Index of directories to start processing at")
    parser.add_argument('-e', '--end', default=None,
                        help="Index of directories to end processing at")
    parser.add_argument('-l', '--list', action="store_true", default=False,
                        help="List all directories and exit")
    parser.add_argument('-c', '--direc', default=None,
                        help="Optionally name a specific directory to edit")
    parser.add_argument('-x', '--overwrite', default=False,
                        help="Overwrite existing ADC data",
                        action='store_true')
    parser.add_argument('-i', '--intermediates', default=False,
                        help="Don't delete intermediates",
                        action='store_true')

    # get arguments and check them
    args = parser.parse_args()
    data_dir = args.data_dir
    spec_direc = args.direc
    if spec_direc:
        assert os.path.isdir(spec_direc), "Specified directory does not exist at {}".format(spec_direc)
    else:
        assert data_dir, "Must specify data directory using param --data_dir"
        assert os.path.isdir(data_dir), "Data directory not found at {}".format(data_dir)

    # start and end
    start = args.start
    end = args.end

    # handle specific directory
    if spec_direc:
        my_direcs = [spec_direc]
    else:
        # list all subdirs with the processed data
        my_direcs = [item for item in glob(data_dir + "/*") if os.path.isdir(item)]
        my_direcs = sorted(my_direcs, key=lambda x: int(os.path.basename(x)))

        # set start and stop for subset/specific diectories only using options below
        if end:
            my_direcs = my_direcs[int(start):int(end) + 1]
        else:
            my_direcs = my_direcs[int(start):]
    if isinstance(my_direcs, str):
        my_direcs = [my_direcs]

    # handle list flag
    if args.list:
        for i, item in enumerate(my_direcs, 0):
            print(str(i) + ': ' + item)
        exit()

    # do work
    results = dwi2adc(my_direcs, repeat=args.overwrite, temp=args.intermediates)
