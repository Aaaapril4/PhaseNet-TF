from collections import OrderedDict
from typing import List, Tuple

import torch
import torch.nn as nn
from lightning import LightningModule

from src.models.loss import focal_loss
from src.models.metrics import F1, Precision, Recall
from src.models.utils.peaks import extract_peaks


class PhaseNetTFModule(LightningModule):
    def __init__(
        self,
        # model params
        net: nn.Module,
        sgram_generator: nn.Module,
        optimizer: torch.optim.Optimizer,
        scheduler: torch.optim.lr_scheduler,
        loss: str = "kl_div",
        # metrics params
        phases: List[str] = ["P", "S", "PS"],
        extract_peaks_sensitive_possibility: List[float] = [0.5, 0.5, 0.3],
        extract_peaks_sensitive_distances_in_seconds: float = 5.0,
        window_length_in_npts: int = 4800,
        dt_s: float = 0.025,
        metrics_true_positive_threshold_s: float = 1.0,
    ):
        super().__init__()

        self.save_hyperparameters(logger=False, ignore=[
                                  "net", "sgram_generator"])

        self.net = net
        self.sgram_generator = sgram_generator

        self.metrics = self._init_metrics(
            phases, window_length_in_npts, dt_s, metrics_true_positive_threshold_s
        )

    def _init_metrics(self, phases, window_length_in_npts, dt_s, metrics_true_positive_threshold_s) -> nn.ModuleDict:
        metrics_dict = OrderedDict()
        threshold = int(metrics_true_positive_threshold_s / dt_s)

        for stage in ["metrics_val", "metrics_test"]:
            metrics_dict[stage] = OrderedDict()
            for iphase, phase in enumerate(phases):
                metrics_dict[stage][phase] = OrderedDict()
                metrics_dict[stage][phase]["precision"] = Precision(
                    iphase, threshold, window_length_in_npts)
                metrics_dict[stage][phase]["recall"] = Recall(
                    iphase, threshold, window_length_in_npts)
                metrics_dict[stage][phase]["f1"] = F1(
                    iphase, threshold, window_length_in_npts)
                metrics_dict[stage][phase] = nn.ModuleDict(
                    metrics_dict[stage][phase])

            metrics_dict[stage] = nn.ModuleDict(metrics_dict[stage])

        return nn.ModuleDict(metrics_dict)

    def forward(self, x: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        sgram = self.sgram_generator(x)
        output = self.net(sgram)
        predict = output['predict']
        return predict, sgram

    def on_train_start(self):
        for phase in self.hparams.phases:
            for key in self.metrics["metrics_val"][phase]:
                self.metrics["metrics_val"][phase][key].reset()

    def model_step(self, batch: dict) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        wave, label = batch["data"], batch["label"]
        predict, sgram = self.forward(wave)
        loss = self.compute_loss(predict, label)
        sgram_power = self.compute_sgram_power(sgram)

        return loss, sgram_power, predict

    def compute_loss(self, predict, label) -> torch.Tensor:
        if self.hparams.loss == "kl_div":
            loss = nn.functional.kl_div(
                nn.functional.log_softmax(predict, dim=1), torch.clamp(label, min=1e-8), reduction='batchmean',
            )
        elif self.hparams.loss == "focal":
            loss = focal_loss(
                nn.functional.softmax(predict, dim=1), label,
            )
        return loss

    def compute_sgram_power(self, sgram) -> torch.Tensor:
        real = sgram[:, :3, :, :]
        imag = sgram[:, 3:, :, :]
        sgram_power = real ** 2 + imag ** 2
        return sgram_power

    def training_step(self, batch: dict, batch_idx: int) -> dict:
        loss, sgram_power, predict = self.model_step(batch)
        peaks = self.extract_peaks_from_predict(predict)

        log_content = {"loss_train": loss}
        self.log_dict(log_content,
                      on_step=False, on_epoch=True, batch_size=len(batch["data"]), sync_dist=True, prog_bar=True)
        return {
            "loss": loss,
            "sgram_power": sgram_power,
            "predict": predict,
            "peaks": peaks,
        }

    def validation_step(self, batch: dict, batch_idx: int) -> dict:
        loss, sgram_power, predict = self.model_step(batch)
        peaks = self.extract_peaks_from_predict(predict)
        predict_arrivals = peaks["arrivals"]

        log_content = {"loss_val": loss}
        self.log_metrics("metrics_val", predict_arrivals,
                         batch["phase_index"], log_content)

        return {
            "val_loss": loss,
            "sgram_power": sgram_power,
            "predict": predict,
            "peaks": peaks,
        }

    def test_step(self, batch: dict, batch_idx: int) -> dict:
        loss, sgram_power, predict = self.model_step(batch)
        peaks = self.extract_peaks_from_predict(predict)
        predict_arrivals = peaks["arrivals"]

        log_content = {"loss_test": loss}
        self.log_metrics("metrics_test", predict_arrivals,
                         batch["phase_index"], log_content)

        return {
            "test_loss": loss,
            "sgram_power": sgram_power,
            "predict": predict,
            "peaks": peaks,
        }

    def extract_peaks_from_predict(self, predict) -> dict:
        sensitive_heights = {k: v for k, v in zip(
            self.hparams.phases, self.hparams.extract_peaks_sensitive_possibility)}
        sensitive_distances = {
            k: self.hparams.extract_peaks_sensitive_distances_in_seconds for k in self.hparams.phases}

        peaks = extract_peaks(nn.functional.softmax(predict, dim=1), self.hparams.phases, sensitive_heights,
                              sensitive_distances, int(1.0 / self.hparams.dt_s))
        return peaks

    def log_metrics(self, stage, predict_arrivals, true_arrivals, log_content):
        for phase in self.metrics[stage]:
            for key in self.metrics[stage][phase]:
                self.metrics[stage][phase][key](
                    predict_arrivals, true_arrivals)
                log_content[f"Metrics/{stage}/{phase}/{key}"] = self.metrics[stage][phase][key]
        self.log_dict(log_content, on_step=False,
                      on_epoch=True, batch_size=len(true_arrivals), sync_dist=True, prog_bar=True)

    def on_test_epoch_end(self):
        metrics = {}
        for phase in self.metrics["metrics_test"]:
            for key in self.metrics["metrics_test"][phase]:
                metrics[f"Metrics/{phase}/{key}"] = self.metrics["metrics_test"][phase][key].compute()
                self.metrics["metrics_test"][phase][key].reset()

        if self.global_rank == 0 and hasattr(self.logger.experiment, "config"):
            # hasattr in case not using wandb
            self.logger.experiment.config.update(metrics)

    def configure_optimizers(self) -> dict:
        optimizer = self.hparams.optimizer(params=self.parameters())
        if self.hparams.scheduler is not None:
            scheduler = self.hparams.scheduler(optimizer=optimizer)
            return {
                "optimizer": optimizer,
                "lr_scheduler": {
                    "scheduler": scheduler,
                    "monitor": "loss_val",
                    "interval": "epoch",
                    "frequency": 1,
                },
            }
        return {"optimizer": optimizer}


if __name__ == "__main__":
    # test code
    from src.models.components.deeplabv3p import DeepLabV3Plus
    from src.models.spectrogram import GenSgram
    from functools import partial

    net = DeepLabV3Plus()
    sgram_generator = GenSgram()
    optimizer = partial(torch.optim.AdamW, lr=0.001,
                        weight_decay=1e-3, amsgrad=False)
    scheduler = partial(torch.optim.lr_scheduler.MultiStepLR,
                        milestones=[30, 60, 90, 120], gamma=0.6)

    module = PhaseNetTFModule(
        net=net,
        sgram_generator=sgram_generator,
        optimizer=optimizer,
        scheduler=scheduler
    )
