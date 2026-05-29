@echo off
copy /b canslim.db.part001+canslim.db.part002+canslim.db.part003+canslim.db.part004+canslim.db.part005+canslim.db.part006+canslim.db.part007 canslim.db
if exist canslim.db (
  echo Rebuilt canslim.db
) else (
  echo Could not rebuild canslim.db
)
pause
