#!/bin/bash
#SBATCH --gpus 1
#SBATCH --cpus-per-gpu 8
#SBATCH --mem-per-gpu 24GB
#SBATCH -p gpu
#SBATCH -t 96:00:00

module load rocky8 micromamba

micromamba activate hifigan

# python filelist.py --input /gpfs/mariana/smbgroup/guitareffectsaire/hifigan_all_1_sec_v2 --output ./filelists/44.1k_audio_filelist_all.txt
# Total time: 11.0h
# python split.py --input ./filelists/44.1k_audio_filelist_all.txt

python train.py --config configs/44.1k_all.json