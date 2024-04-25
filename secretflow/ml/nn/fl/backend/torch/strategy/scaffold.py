# Copyright chenyufan, chenyufan_22@163.com
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import copy
from typing import Tuple
import numpy as np
import torch

from secretflow.ml.nn.fl.backend.torch.fl_base import BaseTorchModel
from secretflow.ml.nn.fl.strategy_dispatcher import register_strategy


class Scaffold(BaseTorchModel):
    def train_step(
        self, weights: np.ndarray, cur_steps: int, train_steps: int, **kwargs
    ) -> Tuple[np.ndarray, int]:
        """Accept ps model params, then do local train

        Args:
            weights: global weight from params server
            cur_steps: current train step
            train_steps: local training steps
            kwargs: strategy-specific parameters
        Returns:
            Parameters after local training
        """
        # Define Scaffold hyperparameters here
        self.model.cg = []
        self.model.c = []
        for param in self.model.parameters():
            self.model.cg.append(torch.zeros_like(param))
            self.model.c.append(torch.zeros_like(param))
        self.model.eta_l = 0.01

        assert self.model is not None, "Model cannot be none, please give model define"
        self.model.train()
        if self.model.c is None:
            self.model.c = self.model.zeros_like()
        refresh_data = kwargs.get("refresh_data", False)
        if refresh_data:
            self._reset_data_iter()
        if weights is not None:
            self.model.update_weights(weights)
        num_sample = 0
        dp_strategy = kwargs.get("dp_strategy", None)
        logs = {}

        optimizer = self.model.optimizers()
        for _ in range(train_steps):
            optimizer.zero_grad()
            iter_data = next(self.train_iter)
            if len(iter_data) == 2:
                x, y = iter_data
                s_w = None
            elif len(iter_data) == 3:
                x, y, s_w = iter_data
            x = x.float()
            num_sample += x.shape[0]
            if len(y.shape) == 1:
                y_t = y
            else:
                if y.shape[-1] == 1:
                    y_t = torch.squeeze(y, -1).long()
                else:
                    y_t = y.argmax(dim=-1)
            if self.use_gpu:
                x = x.to(self.exe_device)
                y_t = y_t.to(self.exe_device)
                if s_w is not None:
                    s_w = s_w.to(self.exe_device)
            y_pred = self.model(x)

            # do back propagation
            loss = self.model.loss(y_pred, y_t.long())
            loss.backward()
            optimizer.step()
            for m in self.model.metrics:
                m.update(y_pred.cpu(), y_t.cpu())
            local_gradients = self.model.get_gradients()
            # Update local model parameters
            for i, it in enumerate(local_gradients):

                it = (torch.Tensor(it) + self.model.c[i] - self.model.cg[i]).tolist()

            model_weights = self.get_weights(return_numpy=True)
            for i in range(len(model_weights)):
                local_gradients[i] = local_gradients[i] * self.model.eta_l
                model_weights[i] -= local_gradients[i]

        # Update c after local training is completed
        for i in range(len(model_weights)):
            model_weights[i] *= 1 / train_steps / self.model.eta_l
        for i, it in enumerate(self.model.c):
            it = torch.Tensor(model_weights[i]) - self.model.cg[i] + it
        loss_value = loss.item()
        logs["train-loss"] = loss_value

        self.logs = self.transform_metrics(logs)
        self.wrapped_metrics.extend(self.wrap_local_metrics())
        self.epoch_logs = copy.deepcopy(self.logs)

        model_weights = self.get_weights(return_numpy=True)

        # DP operation
        if dp_strategy is not None:
            if dp_strategy.model_gdp is not None:
                model_weights = dp_strategy.model_gdp(model_weights)

        return model_weights, num_sample

    def apply_weights(self, weights, **kwargs):
        """Accept ps model params, then update local model

        Args:
            weights: global weight from params server
        """
        if weights is not None:
            self.model.update_weights(weights)


@register_strategy(strategy_name="scaffold", backend="torch")
class PYUScaffold(Scaffold):
    pass
