# Sigverify

Verify the code signature of Windows 10 PE files in memory。


Usage
python vol.py -f <dump file> --profile <profile> sigverify --catalog /path/to/catroot sigcheck --sys(For .sys)
python vol.py -f <dump file> --profile <profile> sigverify --catalog /path/to/catroot sigcheck --dll(For .dll)
python vol.py -f <dump file> --profile <profile> sigverify --catalog /path/to/catroot sigcheck      (For .exe)


Tested Operating Systems
Windows 10x64 14393
Windows 10x64 15063
Windows 10x64 16299
Windows 10x64 17134


Output Example
Module                       Pid Result                                                                                                                  
------------------------- ------ -------------------------------------------------------------
ACPI.sys                       0 Signatue verification: Unable to get local issuer certificate
usbuhci.sys                    0 Verification successful (catalog-signed)
monitor.sys                    0 Verification successful (catalog-signed)
ndistapi.sys                   0 Verification successful (catalog-signed)
spldr.sys                      0 Unable to read FileObject
volsnap.sys                    0 Unable to read FileObject
ntoskrnl.exe                   0 Unable to read FileObject
peauth.sys                     0 Verification successful (catalog-signed)
srvnet.sys                     0 Verification successful (catalog-signed)
srv.sys                        0 Partial file content. Not signed file
vmhgfs.sys                     0 Unable to rebuilt PE file
asyncmac.sys                   0 Verification successful (catalog-signed)
luafv.sys                      0 Verification successful (catalog-signed)
lltdio.sys                     0 Verification successful (catalog-signed)
rspndr.sys                     0 Verification successful (catalog-signed)
