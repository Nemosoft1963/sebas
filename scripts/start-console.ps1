param([switch]$Speak,[switch]$Voice,[switch]$Check)
$a=@()
if($Speak){$a+='--speak'}
if($Voice){$a+='--voice'}
if($Check){$a+='--check'}
& .\.venv\Scripts\python.exe -m app.main @a
