import itertools
import logging
import random
from typing import Any, Dict

import lightning.pytorch as pl
import torch
import torchaudio
import torchaudio.transforms as T
import torchmetrics
from torch import nn, optim
from torch.nn import functional as F

from .. import utils
from ..mel_processing import (
    mel_spectrogram_torch,
    spec_to_mel_torch,
    spectrogram_torch,
    spectrogram_torch_audio,
)
from ..model.commons import (
    clip_grad_value_,
    rand_slice_segments,
    sequence_mask,
    slice_segments,
)
from ..model.discriminators.multi_period_discriminator import MultiPeriodDiscriminator
from ..model.discriminators.multi_scale_discriminator import MultiScaleDiscriminator
from ..model.discriminators.spectrogram_discriminator import SpectrogramDiscriminator
from ..model.generators.generator import Generator
from ..model.pipeline import AudioPipeline
from .losses import discriminator_loss, feature_loss, generator_loss, kl_loss


class HifiGAN(pl.LightningModule):
    def __init__(self, **kwargs):
        super().__init__()
        self.automatic_optimization = False
        self.save_hyperparameters(*[k for k in kwargs])

        self.net_g = Generator(
            self.hparams.model.inter_channels,
            self.hparams.model.resblock_kernel_sizes,
            self.hparams.model.resblock_dilation_sizes,
            self.hparams.model.upsample_rates,
            self.hparams.model.upsample_initial_channel,
            self.hparams.model.upsample_kernel_sizes,
            self.hparams.model.upsample_dilation_sizes,
            self.hparams.model.pre_kernel_size,
            self.hparams.model.post_kernel_size,
        )
        self.net_period_d = MultiPeriodDiscriminator(
            periods=self.hparams.model.multi_period_discriminator_periods,
            use_spectral_norm=self.hparams.model.use_spectral_norm,
        )
        self.net_scale_d = MultiScaleDiscriminator(
            use_spectral_norm=self.hparams.model.use_spectral_norm
        )

        self.audio_pipeline = AudioPipeline(
            freq=self.hparams.data.sampling_rate,
            n_fft=self.hparams.data.filter_length,
            n_mel=self.hparams.data.n_mel_channels,
            win_length=self.hparams.data.win_length,
            hop_length=self.hparams.data.hop_length,
        )
        for param in self.audio_pipeline.parameters():
            param.requires_grad = False

        # metrics
        self.valid_mel_loss = torchmetrics.MeanMetric()

    def training_step(self, batch: Dict[str, torch.Tensor], batch_idx: int):
        discriminator_opt, generator_opt = self.optimizers()

        x_wav, x_wav_lengths = batch["x_wav_values"], batch["x_wav_lengths"]
        y_wav, y_wav_lengths = batch["y_wav_values"], batch["y_wav_lengths"]

        with torch.inference_mode():
            x_mel = self.audio_pipeline(x_wav.squeeze(1), aug=True)
            x_mel_lengths = (x_wav_lengths / self.hparams.data.hop_length).long()

        x_mel, ids_slice = rand_slice_segments(
            x_mel,
            x_mel_lengths,
            self.hparams.train.segment_size // self.hparams.data.hop_length,
        )
        y_wav = slice_segments(
            y_wav,
            ids_slice * self.hparams.data.hop_length,
            self.hparams.train.segment_size,
        )  # slice

        y_spec = spectrogram_torch_audio(
            y_wav.squeeze(1).float(),
            self.hparams.data.filter_length,
            self.hparams.data.sampling_rate,
            self.hparams.data.hop_length,
            self.hparams.data.win_length,
            False,
        )

        # generator forward
        y_hat = self.net_g(x_mel)

        y_mel_hat = mel_spectrogram_torch(
            y_hat.squeeze(1).float(),
            self.hparams.data.filter_length,
            self.hparams.data.n_mel_channels,
            self.hparams.data.sampling_rate,
            self.hparams.data.hop_length,
            self.hparams.data.win_length,
            self.hparams.data.mel_fmin,
            self.hparams.data.mel_fmax,
        )

        # Discriminator
        discriminator_opt.zero_grad()
        # MPD
        y_dp_hat_r, y_dp_hat_g, _, _ = self.net_period_d(y_wav, y_hat.detach())
        loss_disc_p, losses_disc_p_r, losses_disc_p_g = discriminator_loss(
            y_dp_hat_r, y_dp_hat_g
        )

        # MSD
        y_ds_hat_r, y_ds_hat_g, _, _ = self.net_scale_d(y_wav, y_hat.detach())
        loss_disc_s, losses_disc_s_r, losses_disc_s_g = discriminator_loss(
            y_ds_hat_r, y_ds_hat_g
        )

        loss_disc_all = loss_disc_p + loss_disc_s

        # log
        lr = self.optim_g.param_groups[0]["lr"]
        scalar_dict = {"train/d/loss_total": loss_disc_all, "learning_rate": lr}
        scalar_dict.update(
            {"train/d_p_r/{}".format(i): v for i, v in enumerate(losses_disc_p_r)}
        )
        scalar_dict.update(
            {"train/d_p_g/{}".format(i): v for i, v in enumerate(losses_disc_p_g)}
        )
        scalar_dict.update(
            {"train/d_s_r/{}".format(i): v for i, v in enumerate(losses_disc_s_r)}
        )
        scalar_dict.update(
            {"train/d_s_g/{}".format(i): v for i, v in enumerate(losses_disc_s_g)}
        )

        image_dict = {}

        tensorboard = self.logger.experiment

        utils.summarize(
            writer=tensorboard,
            global_step=self.global_step,
            images=image_dict,
            scalars=scalar_dict,
        )

        self.manual_backward(loss_disc_all)
        discriminator_opt.step()

        # Generator
        generator_opt.zero_grad()

        y_dp_hat_r, y_dp_hat_g, fmap_p_r, fmap_p_g = self.net_period_d(y_wav, y_hat)
        loss_p_fm = feature_loss(fmap_p_r, fmap_p_g)
        loss_p_gen, losses_p_gen = generator_loss(y_dp_hat_g)

        y_ds_hat_r, y_ds_hat_g, fmap_s_r, fmap_s_g = self.net_scale_d(y_wav, y_hat)
        loss_s_fm = feature_loss(fmap_s_r, fmap_s_g)
        loss_s_gen, losses_s_gen = generator_loss(y_ds_hat_g)

        y_mel = mel_spectrogram_torch(
            y_wav.squeeze(1).float(),
            self.hparams.data.filter_length,
            self.hparams.data.n_mel_channels,
            self.hparams.data.sampling_rate,
            self.hparams.data.hop_length,
            self.hparams.data.win_length,
            self.hparams.data.mel_fmin,
            self.hparams.data.mel_fmax,
        )

        # mel
        loss_mel = F.l1_loss(y_mel_hat, y_mel) * self.hparams.train.c_mel

        loss_gen_all = (loss_s_gen + loss_s_fm) + (loss_p_gen + loss_p_fm) + loss_mel

        # Logging to TensorBoard by default
        lr = self.optim_g.param_groups[0]["lr"]
        scalar_dict = {"train/g/loss_total": loss_gen_all, "learning_rate": lr}
        scalar_dict.update(
            {
                "train/g/p_fm": loss_p_fm,
                "train/g/s_fm": loss_s_fm,
                "train/g/p_gen": loss_p_gen,
                "train/g/s_gen": loss_s_gen,
                "train/g/loss_mel": loss_mel,
            }
        )

        scalar_dict.update(
            {"train/g/p_gen_{}".format(i): v for i, v in enumerate(losses_p_gen)}
        )
        scalar_dict.update(
            {"train/g/s_gen_{}".format(i): v for i, v in enumerate(losses_s_gen)}
        )

        # image_dict = {
        #     "slice/mel_org": utils.plot_spectrogram_to_numpy(y_mel[0].data.cpu().numpy()),
        #     "slice/mel_gen": utils.plot_spectrogram_to_numpy(y_mel_hat[0].data.cpu().numpy()),
        #     "all/mel": utils.plot_spectrogram_to_numpy(mel[0].data.cpu().numpy())
        # }
        image_dict = {}

        tensorboard = self.logger.experiment
        utils.summarize(
            writer=tensorboard,
            global_step=self.global_step,
            images=image_dict,
            scalars=scalar_dict,
        )

        self.manual_backward(loss_gen_all)
        generator_opt.step()

        # return loss_disc_all, loss_gen_all

    def validation_step(self, batch, batch_idx):
        self.net_g.eval()

        x_wav, x_wav_lengths = batch["x_wav_values"], batch["x_wav_lengths"]
        y_wav, y_wav_lengths = batch["y_wav_values"], batch["y_wav_lengths"]

        with torch.inference_mode():
            x_mel = self.audio_pipeline(x_wav.squeeze(1), aug=False)
            x_mel_lengths = (x_wav_lengths / self.hparams.data.hop_length).long()

        y_spec = spectrogram_torch_audio(
            y_wav.squeeze(1),
            self.hparams.data.filter_length,
            self.hparams.data.sampling_rate,
            self.hparams.data.hop_length,
            self.hparams.data.win_length,
            center=False,
        )
        y_spec_lengths = (y_wav_lengths / self.hparams.data.hop_length).long()

        # remove else
        y_wav_hat = self.net_g(x_mel)
        y_hat_lengths = torch.tensor([y_wav_hat.shape[2]], dtype=torch.long)

        y_mel = spec_to_mel_torch(
            y_spec,
            self.hparams.data.filter_length,
            self.hparams.data.n_mel_channels,
            self.hparams.data.sampling_rate,
            self.hparams.data.mel_fmin,
            self.hparams.data.mel_fmax,
        )
        y_mel_hat = mel_spectrogram_torch(
            y_wav_hat.squeeze(1).float(),
            self.hparams.data.filter_length,
            self.hparams.data.n_mel_channels,
            self.hparams.data.sampling_rate,
            self.hparams.data.hop_length,
            self.hparams.data.win_length,
            self.hparams.data.mel_fmin,
            self.hparams.data.mel_fmax,
        )
        sample_count = min(10, y_wav_hat.size(0))
        image_dict = {}
        audio_dict = {}
        for i in range(sample_count):
            image_dict[f"valid_example_{i}/y_(gen)_mel"] = (
                utils.plot_spectrogram_to_numpy(y_mel_hat[i].cpu().numpy())
            )
            image_dict[f"valid_example_{i}/x_(gt)_mel"] = (
                utils.plot_spectrogram_to_numpy(y_mel[i].cpu().numpy())
            )
            audio_dict[f"valid_example_{i}/y_(gen)_audio"] = (
                y_wav_hat[i, :, : y_hat_lengths[0]].squeeze(0).float()
            )
            audio_dict[f"valid_example_{i}/y_(gt)_audio_"] = (
                y_wav[i, :, : y_wav_lengths[i]].squeeze(0).float()
            )

        mel_mask = torch.unsqueeze(
            sequence_mask(x_mel_lengths.long(), y_mel.size(2)), 1
        ).to(y_mel.dtype)

        # metrics compute
        y_mel_masked = y_mel * mel_mask
        y_mel_masked_hat = y_mel_hat * mel_mask
        valid_mel_loss_step = F.l1_loss(y_mel_masked_hat, y_mel_masked)
        self.valid_mel_loss.update(valid_mel_loss_step.item())
        self.log("valid/loss_mel_step", valid_mel_loss_step.item(), sync_dist=True)

        # logging
        tensorboard = self.logger.experiment
        utils.summarize(
            writer=tensorboard,
            global_step=self.global_step,
            images=image_dict,
            audios=audio_dict,
            audio_sampling_rate=self.hparams.data.sampling_rate,
        )

    def on_validation_epoch_end(self) -> None:
        self.net_g.eval()
        valid_mel_loss_epoch = self.valid_mel_loss.compute()
        self.log("valid/loss_mel_epoch", valid_mel_loss_epoch.item(), sync_dist=True)
        self.valid_mel_loss.reset()

    def configure_optimizers(self):
        self.optim_g = torch.optim.AdamW(
            self.net_g.parameters(),
            self.hparams.train.generator_learning_rate,
            betas=self.hparams.train.betas,
            eps=self.hparams.train.eps,
        )
        self.optim_d = torch.optim.AdamW(
            itertools.chain(
                self.net_period_d.parameters(), self.net_scale_d.parameters()
            ),
            self.hparams.train.discriminator_learning_rate,
            betas=self.hparams.train.betas,
            eps=self.hparams.train.eps,
        )
        self.scheduler_g = torch.optim.lr_scheduler.ExponentialLR(
            self.optim_g, gamma=self.hparams.train.lr_decay
        )
        self.scheduler_g.last_epoch = self.current_epoch - 1
        self.scheduler_d = torch.optim.lr_scheduler.ExponentialLR(
            self.optim_d, gamma=self.hparams.train.lr_decay
        )
        self.scheduler_d.last_epoch = self.current_epoch - 1

        return [self.optim_d, self.optim_g], [self.scheduler_d, self.scheduler_g]

    def on_load_checkpoint(self, checkpoint: Dict[str, Any]) -> None:
        state_dict = checkpoint["state_dict"]
        model_state_dict = self.state_dict()
        is_changed = False
        for k in state_dict:
            if k in model_state_dict:
                if state_dict[k].shape != model_state_dict[k].shape:
                    logging.info(
                        f"Skip loading parameter: {k}, "
                        f"required shape: {model_state_dict[k].shape}, "
                        f"loaded shape: {state_dict[k].shape}"
                    )
                    state_dict[k] = model_state_dict[k]
                    is_changed = True
            else:
                logging.info(f"Dropping parameter {k}")
                is_changed = True

        if is_changed:
            checkpoint.pop("optimizer_states", None)
