# Breast Simulated Gad
Breast Simulated Gad is a 3D fully convolutional deep neural network designed to synthesize fat-saturated gadolinium enahnced T1 weighted breast MR images from pre-contrast images.

# Installation
The following command will clone a copy of breast_simulated_gad to your computer using git:
```bash
git clone https://github.com/ecalabr/breast_simulated_gad.git
```

# Data directory tree setup
Gadnet expects your image data to be in Nifti format with a specific directory tree. The following example starts with any directory (referred to as data_dir).

```bash
data_dir/
```
This is an example of the base directory for all of the image data that you want to use. All subdirectories in this folder should contain individual patient image data.

```bash
data_dir/123456/
```
This is an example of an individual patient study directory. The directory name is typically a patient ID, but can be any folder name that does not contain the "_" character

```bash
data_dir/123456/123456_T1gad.nii.gz
```
This is an example of a single patient image. The image file name must start with the patient ID (i.e. the same as the patient directory name) followed by a "_" character. All Nifti files are expected to be g-zipped.

# Usage
## Train
The following command will train the network using the parameters specified in the param file (-p):
```bash
python train.py -p breast_simulated_gad/breast_simulated_gad_params.json
```

Training outputs will be located in the model directory as specified in the param file.
 
## Predict
The following command will use the trained model weights to predict for a single patient (-s) with ID 123456:
```bash
python predict.py -p breast_simulated_gad/breast_simulated_gad_params.json -s data_dir/123456
```
By default, the predicted output will be placed in the model directory in a subdirectory named "prediction"; however, the user can specify a different output directory using "-o":
```bash
python predict.py -p breast_simulated_gad/breast_simulated_gad_params.json -s data_dir/123456 -o outputs/
```

## Evaluate
The following command will evaluate the trained network using the testing portion of the data as specified in the params file (-p):
```bash
python evaluate.py -p breast_simulated_gad/breast_simulated_gad_params.json
```
By default, the evaluation output will be placed in the model directory in a subdirectory named "evaluation"; however, the user can specify a different output directory using "-o":
```bash
python evaluate.py -p breast_simulated_gad/breast_simulated_gad_params.json -o eval_outputs/
```
Evaluation metrics can be manually specified using "-t":
```bash
python evaluate.py -p breast_simulated_gad/breast_simulated_gad_params.json -t smape ssim logac
```
