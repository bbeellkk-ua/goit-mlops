"""
export_model.py
---------------
Завантажує претреновану модель MobileNet V2 з torchvision.models
(використовуючи сучасний weights=... API), переводить її в eval-режим
та зберігає у форматі TorchScript через torch.jit.trace у model/model.pt.
"""

import os
import torch
from torchvision.models import mobilenet_v2, MobileNet_V2_Weights


MODEL_DIR = "model"
MODEL_PATH = os.path.join(MODEL_DIR, "model.pt")


def main() -> None:
    os.makedirs(MODEL_DIR, exist_ok=True)

    # 1) Завантажуємо модель із сучасним weights API (не pretrained=True)
    weights = MobileNet_V2_Weights.DEFAULT
    model = mobilenet_v2(weights=weights)

    # 2) Режим inference
    model.eval()

    # 3) Dummy input під розмір, який очікує MobileNet V2 (224x224, 3 канали)
    dummy_input = torch.rand(1, 3, 224, 224)

    # 4) TorchScript через trace
    with torch.no_grad():
        traced_model = torch.jit.trace(model, dummy_input)

    # 5) Зберігаємо
    traced_model.save(MODEL_PATH)
    print(f"[OK] TorchScript-модель збережено: {MODEL_PATH}")

    # 6) Sanity check — перевіряємо, що модель дійсно завантажується назад
    loaded = torch.jit.load(MODEL_PATH, map_location="cpu")
    loaded.eval()
    with torch.no_grad():
        out = loaded(dummy_input)
    print(f"[OK] Тестовий forward-pass: output.shape = {tuple(out.shape)}")


if __name__ == "__main__":
    main()
