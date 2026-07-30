# isometric-bratislava

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Preparing the tiles

Measure the overlaps and cut the gapless tile grid:

```bash
.venv/bin/python scripts/stitch.py --cache offsets.json --mosaic mosaic.jpg --preview preview.jpg
```
