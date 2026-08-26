# Object Security Module

SnapKey Vision AI supports externally trained object-security models for jewellery-shop testing.

This phase intentionally does not include local training, dataset upload, epochs, fine tuning, or retraining workers. Train the model externally, for example on Kaggle, then import the exported `.pt` file.

## Initial Object

```text
scissors
```

The architecture is generic so future objects can be added, such as knife, cutter, or pliers.

## Model Storage

Development default:

```text
data/models/object_security/scissors/
```

Packaged Windows default:

```text
C:\ProgramData\SnapKeyVisionAI\data\models\object_security\scissors\
```

Structure:

```text
production/
  <model-id>/
    best.pt
    metadata.json
candidates/
  <candidate-id>/
    best.pt
    metadata.json
registry.json
```

## Kaggle best.pt Flow

1. Open **Object Security Test**.
2. Upload externally trained `best.pt`.
3. The upload is stored as a candidate model.
4. Candidate upload does not activate production.
5. Test the candidate with webcam, RTSP, configured camera, or video source.
6. Activate the candidate after validation.
7. Use rollback if the new production model is not acceptable.

## Runtime Flow

```text
camera frame
optional ROI
optional tiled inference
YOLO object detection
duplicate suppression
temporal confirmation
confirmed event
optional PC beep
event history
```

Default confirmation:

```text
window_frames: 5
required_hits: 3
minimum_confidence: 0.30
cooldown_seconds: 15
```

## Missing Model Behavior

If no production scissors model is active, the app shows:

```text
No custom scissors model installed.
```

It does not claim generic YOLO is the custom scissors detector.
