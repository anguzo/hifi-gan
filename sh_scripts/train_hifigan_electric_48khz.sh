#!/bin/bash
#SBATCH --gpus 1
#SBATCH --cpus-per-gpu 8
#SBATCH --mem-per-gpu 24GB
#SBATCH -p gpu
#SBATCH -t 96:00:00

module load rocky8 micromamba

micromamba activate hifigan

# python filelist.py --input /gpfs/mariana/smbgroup/guitareffectsaire/hifigan_electric_48khz_1_sec_v2 --output ./filelists/48k_audio_filelist_electric.txt
# Total time: 7.0h
# python split.py --input ./filelists/48k_audio_filelist_electric.txt

python train.py --config configs/48k_electric.json