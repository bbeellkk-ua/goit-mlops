"""
app/inference.py
----------------
Скрипт для inference на TorchScript-моделі (MobileNet V2).

Використання:
    python3 app/inference.py <шлях-до-зображення>

Скрипт:
    * приймає шлях до зображення як CLI-аргумент;
    * завантажує TorchScript-модель з model/model.pt (з підтримкою кількох шляхів
      — і локальний запуск, і Docker-контейнер);
    * застосовує офіційний preprocessing через weights.transforms();
    * виконує inference без градієнтів;
    * друкує top-3 передбачення (class_id, class_name, confidence).
"""

import os
import sys
from pathlib import Path

import torch
from PIL import Image
from torchvision.models import MobileNet_V2_Weights


TOP_K = 3


def resolve_model_path() -> Path:
    """Знайти model.pt у типових розташуваннях (локально або в Docker)."""
    candidates = [
        Path("model/model.pt"),               # запуск з кореня репо
        Path("/app/model/model.pt"),          # у контейнері (WORKDIR=/app)
        Path(__file__).resolve().parent.parent / "model" / "model.pt",  # відносно app/
    ]
    for c in candidates:
        if c.is_file():
            return c
    raise FileNotFoundError(
        "Не знайдено model/model.pt. Спочатку запустіть: python3 export_model.py"
    )


def load_model() -> torch.jit.ScriptModule:
    model_path = resolve_model_path()
    model = torch.jit.load(str(model_path), map_location="cpu")
    model.eval()
    return model


def preprocess_image(image_path: str) -> torch.Tensor:
    """Правильний preprocessing через weights.transforms()."""
    weights = MobileNet_V2_Weights.DEFAULT
    transforms = weights.transforms()

    image = Image.open(image_path).convert("RGB")
    tensor = transforms(image).unsqueeze(0)  # batch dim
    return tensor


def predict(image_path: str, top_k: int = TOP_K):
    if not os.path.isfile(image_path):
        raise FileNotFoundError(f"Файл зображення не знайдено: {image_path}")

    weights = MobileNet_V2_Weights.DEFAULT
    categories = weights.meta.get("categories", [])

    model = load_model()
    input_tensor = preprocess_image(image_path)

    with torch.no_grad():
        logits = model(input_tensor)
        probs = torch.nn.functional.softmax(logits, dim=1)[0]
        confidences, class_ids = torch.topk(probs, k=top_k)

    results = []
    for rank, (cid, conf) in enumerate(zip(class_ids.tolist(), confidences.tolist()), start=1):
        name = categories[cid] if 0 <= cid < len(categories) else f"class_{cid}"
        results.append((rank, cid, name, conf))

    return results


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python3 app/inference.py <image_path>")
        sys.exit(1)

    image_path = sys.argv[1]
    results = predict(image_path, top_k=TOP_K)

    print(f"Image: {image_path}")
    print(f"Top-{TOP_K} predictions (MobileNet V2, ImageNet):")
    for rank, cid, name, conf in results:
        print(f"  #{rank}: class_id={cid:>4}  confidence={conf:.4f}  name={name}")


if __name__ == "__main__":
    main()
