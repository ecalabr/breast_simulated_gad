""" QC preprocessed mammography MRI data """

import os
from glob import glob
import argparse
import psutil
import sys
import time


# define functions
# function for checking for running processes of ITK snap
def check_itk_running(name='ITK-SNAP'):
    # Iterate over the all the running process
    for proc in psutil.process_iter():
        try:
            # Check if process name contains the given name string.
            if name.lower() in proc.name().lower():
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    return False


# image QC in ITK snap
def mammo_qc(direcs, anat_suffix, mask_suffix):

    # handle suffixes without extension
    anat_suffix = [item + '.nii.gz' if not item.endswith('.nii.gz') else item for item in anat_suffix]
    mask_suffix = mask_suffix + '.nii.gz' if not mask_suffix.endswith('.nii.gz') else mask_suffix

    # define outputs
    cmds = []

    # get number of direcs and announce
    n_total = len(direcs)
    print("Performing mask editing for a total of " + str(n_total) + " directories")

    # run ITK-snap on each one
    for ind, direc in enumerate(direcs, 1):
        # check that all files exist
        mask = glob(direc + "/*" + mask_suffix)[0]
        anatomy = [glob(direc + "/*" + item)[0] for item in anat_suffix]
        if all(os.path.isfile(f) for f in [mask] + anatomy):
            cmd = "itksnap --geometry 1920x1080 -g " + anatomy[0] + " -s " + mask + " -o"
            for a in anatomy[1:]:
                cmd = cmd + " " + a

            # run ITK-snap command
            # print(cmd)
            os.system(cmd)

            # if script is running on Mac OS, will need to check for running process (Not an issue on linux)
            # this prevents a new ITK-snap window from opening until the previous one has closed
            if sys.platform == 'darwin':
                print("The next ITK-SNAP window will not open until all open instances of ITK-SNAP have terminated...")
                running = True
                while running:
                    running = check_itk_running()
                    time.sleep(2)

            # report done with this study
            print("Done with study " + os.path.basename(direc) + ": " + str(ind) + " of " + str(n_total))
            cmds.append(cmd)
        else:
            print("Skipping study " + os.path.basename(direc) + ", which is missing data.")

    return cmds


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
    parser.add_argument('-m', '--mask', default="breast_mask.nii.gz",
                        help="Suffix of the mask to be edited")
    parser.add_argument('-a', '--anat',
                        default=['T1_wm', 'T1gad_wm', 'T2FS_wm', 'DWI_wm', 'T1FS_wm', 'B0_wm', 'ADC_wm'],
                        help="Suffixes of additional images to QC",
                        nargs='+')
    parser.add_argument('-c', '--direc', default=None,
                        help="Optionally name a specific directory to edit")

    # get arguments and check them
    args = parser.parse_args()
    data_dir = args.data_dir
    spec_direc = args.direc
    if spec_direc:
        assert os.path.isdir(spec_direc), "Specified directory does not exist at {}".format(spec_direc)
    else:
        assert data_dir, "Must specify data directory using param --data_dir"
        assert os.path.isdir(data_dir), "Data directory not found at {}".format(data_dir)

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

    # handle anatomy images argument
    if not isinstance(args.anat, list):
        args.anat = [args.anat]

    # do work
    commands = mammo_qc(my_direcs, args.anat, args.mask)
