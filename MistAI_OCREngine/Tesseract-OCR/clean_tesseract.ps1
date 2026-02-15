# === CLEANUP SCRIPT FOR MISTAI OCR ===

# Delete unnecessary executables (training, docs, uninstallers)
$deleteFiles = @(
"cntraining.exe","mftraining.exe","lstmtraining.exe","lstmeval.exe","text2image.exe",
"shapeclustering.exe","merge_unicharsets.exe","unicharset_extractor.exe",
"combine_lang_model.exe","combine_tessdata.exe","classifier_tester.exe",
"dawg2wordlist.exe","wordlist2dawg.exe","tesseract-uninstall.exe","winpath.exe"
)

foreach ($file in $deleteFiles) {
    if (Test-Path $file) { Remove-Item $file -Force }
}

# Delete docs and HTML files
Remove-Item *.html -ErrorAction SilentlyContinue
Remove-Item doc -Recurse -Force -ErrorAction SilentlyContinue

# Keep only English + osd traineddata
Get-ChildItem tessdata -Exclude eng.traineddata,osd.traineddata | Remove-Item -Force

Write-Host "✅ Tesseract cleaned! Only runtime and tessdata remain."
