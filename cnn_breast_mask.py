""" hacky script for making a bunch of breast masks using a CNN """

import argparse
import os
from glob import glob
from prob2seg import convert_prob
import logging
from utilities.utils import Params
from predict import predict


# set tensorflow logging to FATAL before importing
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'  # 0 = INFO, 1 = WARN, 2 = ERROR, 3 = FATAL
logging.getLogger('tensorflow').setLevel(logging.FATAL)


# define function to make a batch of brain masks from a list of directories
def batch_mask(infer_direcs, param_files, out_dir, suffix, overwrite=False, thresh=0.5):
    # ensure that infer_direcs is list
    if not isinstance(infer_direcs, (list, tuple)):
        infer_direcs = [infer_direcs]
    # ensure that param_files is list
    if not isinstance(param_files, (list, tuple)):
        param_files = [param_files]
    # initiate outputs
    outnames = []
    # run inference and post-processing for each infer_dir
    for direc in infer_direcs:
        # inner for loop for multiple models
        probs = []
        for param_file in param_files:
            # load params and determine model dir
            params = Params(param_file)
            if params.model_dir == 'same':  # this allows the model dir to be inferred from params.json file path
                params.model_dir = os.path.dirname(param_file)
            if not os.path.isdir(params.model_dir):
                raise ValueError("Specified model directory does not exist")
            # run predict on one directory and get the output probabilities
            prob = predict(params, [direc], out_dir, mask=None, checkpoint='last')  # direc must be list for predict fn
            probs.append(prob[0])  # output of predict fn is a list, this converts back to string so its not nested list

        # convert probs to mask with cleanup
        idno = os.path.basename(direc.rsplit('/', 1)[0] if direc.endswith('/') else direc)
        nii_out_path = os.path.join(direc, idno + "_" + suffix + ".nii.gz")
        if os.path.isfile(nii_out_path) and not overwrite:
            print("Mask file already exists at {}".format(nii_out_path))
        else:
            if probs:
                nii_out_path = convert_prob(probs, nii_out_path, clean=True, thresh=thresh)

                # report
                if os.path.isfile(nii_out_path):
                    print("- Created mask file at: {}".format(nii_out_path))
                else:
                    raise ValueError("No mask output file found at: {}".format(direc))

                # add to outname list
                outnames.append(nii_out_path)

    return outnames


# executed  as script
if __name__ == '__main__':
    # parse input arguments
    parser = argparse.ArgumentParser()
    parser.add_argument('-p', '--param_file', default=None,
                        help="Path to params.json")
    parser.add_argument('-m', '--model_dir', default=None,
                        help="Path to model directory containing multiple param files. (optional, replaces param_file)")
    parser.add_argument('-i', '--infer_dir', default=None,
                        help="Path to parent directory or single directory containing the images for inference")
    parser.add_argument('-o', '--out_dir', default=None,
                        help="Optionally specify temporary directory. Default is model directory.")
    parser.add_argument('-s', '--start', default=0,
                        help="Index of directories to start processing at")
    parser.add_argument('-e', '--end', default=None,
                        help="Index of directories to end processing at")
    parser.add_argument('-l', '--list', action="store_true", default=False,
                        help="List the directories to be processed in order then exit")
    parser.add_argument('-x', '--overwrite', action="store_true", default=False,
                        help="Overwrite existing brain mask")
    parser.add_argument('-t', '--thresh', default=0.5,
                        help="Probability threshold for predictions")
    parser.add_argument('-u', '--suffix', default="breast_mask",
                        help="Filename suffix for output mask")
    parser.add_argument('-f', '--force_cpu', default=False,
                        help="Disable GPU and force all computation to be done on CPU",
                        action='store_true')

    # handle model_dir argument
    args = parser.parse_args()
    if args.model_dir:
        my_param_files = glob(args.model_dir + '/*/params.json')
        if not my_param_files:
            raise ValueError("No parameter files found in model directory {}".format(args.model_dir))
    else:
        # handle params argument
        if args.param_file is None:
            print("No param file specified, attempting to find param file in script directory...")
            args.param_file = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'breast_mask/mask.json')
        assert os.path.isfile(args.param_file), "No json configuration file found at {}".format(args.param_file)
        my_param_files = [args.param_file]
    for f in my_param_files:
        print(f)

    # handle out_dir argument
    if args.out_dir:
        assert os.path.isdir(args.out_dir), "Specified output directory does not exist: {}".format(args.out_dir)
    else:
        args.out_dir = os.path.join(os.path.dirname(args.param_file), 'predictions')
        if not os.path.isdir(args.out_dir):
            os.mkdir(args.out_dir)

    # handle inference directory argument
    assert args.infer_dir, "No infer directory specified. Use --infer_dir"
    assert os.path.isdir(args.infer_dir), "No inference directory found at {}".format(args.infer_dir)

    # check if provided dir is a single image dir or a dir full of image dirs
    if glob(args.infer_dir + '/*.nii.gz'):
        infer_dirs = [args.infer_dir]
    elif glob(args.infer_dir + '/*/*.nii.gz'):
        infer_dirs = sorted(list(set([os.path.dirname(f)
                                      for f in glob(args.infer_dir + '/*/*.nii.gz')])))
    else:
        raise ValueError("No image data found in inference directory: {}".format(args.infer_dir))

    # handle thresh argument
    if not isinstance(args.thresh, float):
        try:
            args.thresh = float(args.thresh)
        except:
            raise ValueError("Could not cast thresh argument to float: {}".format(args.thresh))

    # handle suffix argument
    if not isinstance(args.suffix, str):
        raise ValueError('Suffix argument must be a string: {}'.format(args.suffix))
    else:
        if args.suffix.endswith('.nii.gz'):
            args.suffix = args.suffix.split('.nii.gz')[0]

    # handle start and end arguments
    if args.end:
        infer_dirs = infer_dirs[int(args.start):int(args.end)+1]
    else:
        infer_dirs = infer_dirs[int(args.start):]

    # handle list argument
    if args.list:
        for i, item in enumerate(infer_dirs, 0):
            print(str(i) + ': ' + item)
        exit()

    # make sure all input directories have the required input images
    my_params = Params(my_param_files[0])
    data_prefixes = [str(item) for item in my_params.data_prefix]
    compl_infer_dirs = []
    for inf_dir in infer_dirs:
        if all([glob(inf_dir + '/*' + prefix + '.nii.gz') for prefix in data_prefixes]):
            compl_infer_dirs.append(inf_dir)
        else:
            print("Skipping {} which does not have all the required images.".format(inf_dir))

    # handle force cpu argument
    if args.force_cpu:
        logging.info("Forcing CPU (GPU disabled)")
        os.environ["CUDA_VISIBLE_DEVICES"] = "-1"

    # do work
    output_names = batch_mask(compl_infer_dirs,
                              my_param_files,
                              args.out_dir,
                              args.suffix,
                              overwrite=args.overwrite,
                              thresh=args.thresh)
