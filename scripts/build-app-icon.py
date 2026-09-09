"""Package the generated master into Windows icon sizes (no artwork changes)."""
from pathlib import Path
from PIL import Image
root=Path(__file__).resolve().parents[1]/'assets'/'app'
with Image.open(root/'icon-master.png') as image:
    image.convert('RGBA').save(root/'app.ico',sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
