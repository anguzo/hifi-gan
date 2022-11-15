import torch, torchaudio
import os
import glob
from hifigan.mel_processing import mel_spectrogram_torch, spectrogram_torch_audio
from hifigan.model.hifigan import HifiGAN

def load_local():
    ckpt_path = None
    if os.path.exists("logs/lightning_logs"):
        versions = glob.glob("logs/lightning_logs/version_*")
        if len(list(versions)) > 0:
            last_ver = sorted(list(versions))[-1]
            last_ckpt = os.path.join(last_ver, "checkpoints/last.ckpt")
            if os.path.exists(last_ckpt):
                ckpt_path = last_ckpt
    
    print(ckpt_path)
    
    model = HifiGAN.load_from_checkpoint(checkpoint_path=ckpt_path, strict=False)

    return model.net_g

def load_remote():
    return torch.hub.load("vtuber-plan/hifi-gan:main", "hifigan_48k", force_reload=True)

# Load Remote checkpoint
hifigan = load_remote().cuda()

# Load Local checkpoint
# hifigan = load_local().cuda()

# Load audio
wav, sr = torchaudio.load("test.wav")
assert sr == 48000

mel = mel_spectrogram_torch(wav, 2048, 256, 48000, 512, 2048, 0, None, False)

mel = mel.cuda()
out = hifigan(mel)

wav_out = out.squeeze(0).cpu()

torchaudio.save("test_out.wav", wav_out, sr)