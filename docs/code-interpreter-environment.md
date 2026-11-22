# Azure code interpreter environment (measured 2026-11-21)

`pip list` inside the `gpt-6-luna` code-interpreter container on this subscription reported
**404 installed packages**. Microsoft documents the *supported file types* and says "support for
popular Python libraries", but publishes no exhaustive, versioned package manifest — so this list is
the reliable source, and it can drift with the Azure image.

## Verified present (selection)

pandas, numpy, matplotlib, scipy, scikit-learn, seaborn, statsmodels, sympy, plotly,
openpyxl, xlsxwriter, python-docx, python-pptx, pypdf, PyPDF2, pdfplumber, PyMuPDF (fitz),
beautifulsoup4, lxml, Pillow, geopandas, shapely, networkx, sqlalchemy, reportlab, tabulate,
requests, aiohttp, GitPython, Flask.

## Supported input formats (Microsoft)

.c .cpp .csv .docx .html .java .json .md .pdf .php .pptx .py .rb .tex .txt .css .jpeg .jpg .js
.gif .png .tar .ts .xlsx .xml .zip

Source: https://learn.microsoft.com/en-us/azure/ai-foundry/openai/how-to/responses

## Getting the live list

```python
import importlib.metadata as md
print(sorted({d.metadata['Name'] for d in md.distributions() if d.metadata['Name']}))
```

Ask `run_code` for this once per container when an exact package/version matters.
