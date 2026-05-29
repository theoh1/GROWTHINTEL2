DATABASE CHUNKS

Upload all canslim.db.part files to the backend server.
They must be joined back together into one file called:
canslim.db

On Windows, put all part files in one folder and double-click:
REBUILD canslim.db.bat

On a Linux server, run this in the folder with the parts:
cat canslim.db.part* > canslim.db

Then place canslim.db in the backend API folder before starting the backend.
