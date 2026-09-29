# Backup Guide

## Device-Specific
### iOS
As jailbreaking is not as prevalant anymore, use a third-party program to export Phira's data from a device backup.<br>
File path (from backup): `/Container/Library/data/charts/`

### Android
Root access is a lot easier on Android, hence Phira's data is easier to access. You may also use MTManager to mount Phira to an accessible directory without root.<br>
File path (from Phira): `[root] data/data/org.flos.phira/data/charts/`

### Windows
Easiest of them all. Data is stored plainly.
File path (from Phira directory): `data/charts/`

## Notes
- Online-downloaded charts are stored in `/data/charts/download`. `data/charts/custom` contains any imported charts.
- Within `/data/charts/download` there are probably dozens of empty folders with random UUID names. Those are safe to delete; that UUID also doesn't point anywhere useful on the Phira server.
