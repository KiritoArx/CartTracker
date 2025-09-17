import pytesseract
from PIL import Image

# Tell Python where Tesseract is installed
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# Load an image (replace with any screenshot path you have)
img = Image.open("sample.png")

# Extract text
text = pytesseract.image_to_string(img)
print("Detected text:")
print(text)
