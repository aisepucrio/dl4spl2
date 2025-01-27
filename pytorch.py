#!/usr/bin/env python
# coding: utf-8

# Importar bibliotecas
import torch
from torch import nn
import lightning as L
from pytorch_lightning import Trainer
from pytorch_lightning.callbacks import ModelCheckpoint

from ray import train, tune, put, get, init
from ray.tune.search.optuna import OptunaSearch
from ray.tune.integration.pytorch_lightning import TuneReportCheckpointCallback
from ray.tune.schedulers import ASHAScheduler
from functools import partial

import matplotlib.pyplot as plt
import seaborn as sns

import mlflow
import numpy as np
import pandas as pd
import os
from sklearn.model_selection import train_test_split

import logging
import sys
import multiprocessing

logging.basicConfig(level=logging.INFO, filename="training_logs.log", filemode="a", format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Carregar e preprocessar o dataset
df = pd.read_parquet('data.parquet')
df = df.astype('float32')
df = df.sample(1500)  # Substituir para o dataset completo, se necessário

# Separar features e target
target_column = 'perf'
y = df[target_column]
X = df.drop(columns=[target_column])

X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

# Converter para tensores
X_train_tensor = torch.tensor(X_train.values, dtype=torch.float32)
X_test_tensor = torch.tensor(X_test.values, dtype=torch.float32)
y_train_tensor = torch.tensor(y_train.values, dtype=torch.float32)
y_test_tensor = torch.tensor(y_test.values, dtype=torch.float32)

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
print(device)

# Dataset personalizado
class CustomDataset(torch.utils.data.Dataset):
    def __init__(self, features, labels):
        self.features = features
        self.labels = labels
    
    def __len__(self):
        return len(self.features)
    
    def __getitem__(self, idx):
        return self.features[idx], self.labels[idx]

# Criar DataLoaders
train_dataset = CustomDataset(X_train_tensor, y_train_tensor)
test_dataset = CustomDataset(X_test_tensor, y_test_tensor)

num_workers = min(8, multiprocessing.cpu_count())  # Detectar automaticamente o número de núcleos disponíveis, limitado a 8

train_dataloader = torch.utils.data.DataLoader(train_dataset, batch_size=1024, shuffle=True, num_workers=num_workers)
test_dataloader = torch.utils.data.DataLoader(test_dataset, batch_size=1024, shuffle=False, num_workers=num_workers)

# Definir o modelo
class LightningModel(L.LightningModule):
    def __init__(self, num_features, activation="ReLU", optimizer_name="Adam", loss_name="MSE"):
        super().__init__()
        self.num_features = num_features
        self.activation = activation
        self.optimizer_name = optimizer_name
        self.loss_name = loss_name
        self.model = self.build_model()

    def forward(self, x):
        return self.model(x)

    def training_step(self, batch, batch_idx):
        x, y = batch
        z = self(x)
        loss = self.get_loss_function()(z, y.unsqueeze(1))
        self.log('train_loss', loss)
        return loss
    
    def validation_step(self, batch, batch_idx):
        x, y = batch
        z = self(x)
        loss = self.get_loss_function()(z, y.unsqueeze(1))
        self.log('val_loss', loss)
        return loss

    def configure_optimizers(self):
        return self.get_optimizer()

    def get_optimizer(self):
        optimizers = {
            "Adam": torch.optim.Adam(self.parameters(), lr=0.001),
            "AdamW": torch.optim.AdamW(self.parameters(), lr=0.001),
        }
        return optimizers[self.optimizer_name]

    def get_loss_function(self):
        loss_functions = {
            "MSE": nn.MSELoss(),
            "SmoothL1Loss": nn.SmoothL1Loss(),
            "MAE": nn.L1Loss()
        }
        return loss_functions[self.loss_name]

    def get_activation(self):
        activations = {
            "ReLU": nn.ReLU(),
            "PReLU": nn.PReLU(),
            "ELU": nn.ELU()
        }
        return activations[self.activation]

    def build_model(self):
        hidden_size = self.num_features // 2
        hidden_size2 = hidden_size // 2
        return nn.Sequential(
            nn.Linear(self.num_features, hidden_size),
            self.get_activation(),
            nn.Dropout(0.3),
            nn.Linear(hidden_size, hidden_size2),
            self.get_activation(),
            nn.Dropout(0.3),
            nn.Linear(hidden_size2, 1)
        )

# Função para treinar o modelo
def train_model_tune(config, num_features, train_dataloader, val_dataloader, max_epochs=100):
    model = LightningModel(
        num_features=num_features,
        activation=config["activation"],
        optimizer_name=config["optimizer"],
        loss_name=config["loss_function"]
    )

    metrics = {"loss": "val_loss"}
    checkpoint_callback = ModelCheckpoint(
        dirpath="checkpoints",
        filename="epoch-{epoch:02d}-val_loss-{val_loss:.2f}",
        save_top_k=5,  # Manter apenas os 5 melhores checkpoints para economizar espaço
        verbose=True,
        every_n_epochs=1
    )

    callbacks = [TuneReportCheckpointCallback(metrics, on="validation_end"), checkpoint_callback]

    # Detectar ambiente de execução
    is_headless = not sys.stdout.isatty()

    trainer = L.Trainer(
        max_epochs=max_epochs,
        accelerator='auto',
        devices='auto',  # Seleciona automaticamente os recursos disponíveis
        callbacks=callbacks,
        enable_progress_bar=not is_headless  # Desativar barra de progresso em ambientes headless
    )
    
    trainer.fit(model, train_dataloader, val_dataloader)

# Função para otimizar hiperparâmetros
def tune_hyperparameters(num_features, train_dataloader, val_dataloader, num_samples=10):
    config = {
        "optimizer": tune.choice(["Adam", "AdamW"]),
        "loss_function": tune.choice(["MAE", "MSE", "SmoothL1Loss"]),
        "activation": tune.choice(["ReLU", "PReLU", "ELU"]),
        "max_t": tune.grid_search([20, 30, 40]),  
        "grace_period": tune.grid_search([5, 10, 15])  
    }
    
    search_algo = OptunaSearch(metric="loss", mode="min")
    scheduler = ASHAScheduler(
        time_attr='training_iteration',
        max_t=max(config['max_t']),
        grace_period=min(config['grace_period']),
        reduction_factor=2
    )
    
    tuner = tune.Tuner(
        partial(train_model_tune, num_features=num_features, train_dataloader=train_dataloader, val_dataloader=val_dataloader),
        tune_config=tune.TuneConfig(
            metric="loss",
            mode="min",
            search_alg=search_algo,
            scheduler=scheduler,
            num_samples=num_samples
        ),
        param_space=config
    )
    
    results = tuner.fit()
    best_result = results.get_best_result(metric="loss", mode="min")
    best_trial_config = best_result.config
    best_trial_loss = best_result.metrics['loss']
    print("Melhor configuração encontrada:", best_trial_config)
    print("Melhor perda na validação:", best_trial_loss)
    
    return best_trial_config

# Executar a otimização de hiperparâmetros
if __name__ == "__main__":
    best_config = tune_hyperparameters(
        num_features=X.shape[1],
        train_dataloader=train_dataloader,
        val_dataloader=test_dataloader,
        num_samples=18
    )
