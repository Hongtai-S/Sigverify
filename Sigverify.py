import os
import re
import json
import pefile
import struct
import sigvalidator

from io import BytesIO
from enum import Enum

import volatility.debug as debug
import volatility.utils as utils
import volatility.win32.tasks as tasks
import volatility.win32.modules as modules
import volatility.plugins.filescan as filescan
import volatility.plugins.dumpfiles as dumpfiles
import volatility.plugins.malware.devicetree as dtree

from volatility.renderers import TreeGrid
from volatility.plugins.common import AbstractWindowsCommand

CERTIFICATE_REGEX = re.compile(b'\x30.\x30.\x06.(?P<oid_algorithm>.{5,9})\x05\x00\x04(?P<hash_size>.)')
OPENSSL_REGEX = re.compile(r' *(?P<offset>[0-9]+):d=[0-9]+ +hl=(?P<header_length>[0-9]+) +l= *(?P<length>[0-9]+)')

class ReturnCode(Enum):
    FILEOBJECT_ERROR = (1, 'Unable to read FileObject')
    PE_REBUILT_FAILED = (2, 'Unable to rebuilt PE file')
    PE_CHECKSUM_MISMATCH = (3, 'PE OptionalHeader.CheckSum mismatch')
    PARTIAL_CONTENT_PE_DATA_ERROR = (4, 'Partial file content. Unable to load PE')
    SIGNED_FILE_NOT_VERIFIED = (5, 'Signed file, but not verified')
    CONTENT_SIGNED_NOT_VERIFIED = (6, 'Partial file content. Signed file, but not verified')
    PARTIAL_CONTENT_MAYBE_CATALOG_SIGNED = (7, 'Partial file content. Not signed file (maybe catalog-signed?)')
    PARTIAL_CONTENT_NOT_SIGNED = (8, 'Partial file content. Not signed file')
    AUTHENTICODE_SIGNATURE_MISMATCH_OR_INCORRECT_IMAGEBASE = (9, 'Certificate\'s hash mismatch calculated hash, or incorrect ImageBase during reconstruction')
    AUTHENTICODE_SIGNATURE_MISMATCH = (10, 'Certificate\'s hash mismatch calculated hash')
    CATALOG_SIGNED = (11, 'Verification successful (catalog-signed)')
    MAYBE_CATALOG_SIGNED = (12, 'Not signed file (maybe catalog-signed?)')
    NOT_SIGNED_OR_INCORRECT_IMAGEBASE = (13, 'Not signed file, or incorrect ImageBase during reconstruction')
    NOT_SIGNED = (14, 'Not signed file')
    NOT_PEB = (15, 'Unable to read PEB')
    ALREADY_TERMINATED = (16, 'Already terminated')
    PARTIAL_CERTIFICATE = (17, 'Embedded certificate incomplete')
    PARTIAL_CONTENT_VERIFIED = (18, 'Partial file content. Unable to compare file hash and signature hash')

    def __int__(self):
        return self.value[0]

    def __str__(self):
        return self.value[1]

class SigVerify(AbstractWindowsCommand):
    '''
    Aims to validate Authenticode-signed processes, either with embedded signature or catalog-signed

    Options:
        --catalog [dir]: directory containing catalog files (.cat), default to '$PWD/catroot/$VOL_PROFILE'
        --dll: verify library modules (.dll)
        --sys: verify driver modules (.sys)
    '''

    def __init__(self, config, *args, **kwargs):
        '''
        Plugin creation
        '''

        AbstractWindowsCommand.__init__(self, config, *args, **kwargs)

        self.__plugin_name, _ = os.path.splitext(os.path.basename(__file__))
        default_catlog_dir = os.path.join('catroot', self._config.get_value("PROFILE"))
        self._config.add_option('CATALOG', help='Catalog dir to search signature into, default to \'$PWD/catroot/$VOL_PROFILE\'', action='store', type='string', default=default_catlog_dir)
        self._config.add_option('DLL', help='Verify DLL modules (.dll)', action='store_true')
        self._config.add_option('SYS', help='Verify driver modules (.sys)', action='store_true')
        self.addr_space = utils.load_as(self._config)

        self.files = []
        self.frequent_addresses = self.load_frequent_addresses()
        # Simple cache
        self.already_analyzed = {}
        self.sigv = None

    def load_frequent_addresses(self):
        try:
            address_file = 'addresses.json'
            profile = self._config.get_value("PROFILE")
            data = self.load_json(address_file)

            for key in data.keys():
                if profile == key:
                    return data[key]

            self.__debug_message('warning', '\'{0}\': Profile not found, trying to reconstruct ImageSectionObjects with \'{1}\''.format(profile, key))
            return data[key]
        except IOError:
            self.__debug_message('error', 'Unable to load most frequent addresses (\'{0}\' file) '.format(address_file))

    def load_json(self, path):
        with open(path, 'r') as f:
            return json.load(f)

    def check_args(self):
        if not os.path.isdir(self._config.catalog):
            self.__debug_message('error', '\'{0}\': Not a directory (--catalog or -h)'.format(os.path.realpath(self._config.catalog)))

        if self._config.DLL and self._config.SYS:
            self.__debug_message('error', 'Incompatible options: either exe (default), or exe with dll (--dll), or sys (--sys)')

    def calculate(self):
        '''
        Main plugin function

        '''

        self.check_args()
        self.sigv = sigvalidator.SigValidator(self._config.catalog)
        modules = []
        self.count = 0
        self.count1 = 0

        if self._config.SYS:
            modules += self.get_sys_modules()
        else:
            for task in tasks.pslist(self.addr_space):
                modules += self.get_pe_modules(task, dlls=self._config.DLL)

        if modules:
            self.files = self.get_files()

            for module in modules:
                module_path, module_name, pid = module
                if module_path in self.already_analyzed:
                    yield module_name, pid, self.already_analyzed[module_path]
                else:
                    if module_path:
                        is_complete, file_object = self.get_file_object(module_path)
                        # We found a complete FileObject to work on
                        if is_complete:
                            result = self.validate_file(file_object)
                            self.already_analyzed[module_path] = result
                            yield module_name, pid, result
                        # We are restricted to likely find signature in last page
                        else:
                            result = self.validate_partial_file(file_object)
                            self.already_analyzed[module_path] = result
                            yield module_name, pid, result
                    # Sometimes, terminated processes are still listed
                    elif task.ExitTime:
                        yield task.ImageFileName, pid, ReturnCode.ALREADY_TERMINATED
                    else:
                        yield task.ImageFileName, pid, ReturnCode.NOT_PEB

    def get_files(self):
        '''
        Uses FileScan plugin to retrieve all FileObjects of memory dump

        '''

        self.__debug_message('info', 'Retrieving all file objects, this may take a while...\n')

        ret = []
        self._config.DUMP_DIR = '.'
        dumper = dumpfiles.DumpFiles(self._config)
        for fileobj in dumper.calculate():
            try:
                filename = fileobj['name']
                types = fileobj['type']
                if fileobj['pid']:
                    pid = int(fileobj['pid'])
                else:
                    pid = None
                present = fileobj['present']
                pad = fileobj['pad']
                ret += [{'name': filename, 'type': types,'pid':pid,'present':present,'pad':pad}]
            except KeyError:
                try:
                    self.__debug_message('warning', 'FileObject \'{0}\': SharedCacheMap not supported'.format(fileobj['name'].split('\\')[-1]))
                except AttributeError:
                    pass
        return ret

    def get_pe_modules(self, task, dlls=False):
        '''
        Gets executable full paths and base name

        '''

        ret = []

        if dlls:
            for mod in task.get_load_modules():
                ret += [(str(mod.FullDllName), str(mod.BaseDllName), int(task.UniqueProcessId))]
        else:
            for mod in task.get_load_modules():
                # Return first module (.exe in InLoadOrderModuleList)
                return [(str(mod.FullDllName), str(mod.BaseDllName), int(task.UniqueProcessId))]

        return ret

    def get_sys_modules(self):
        ret = []

        modlist = list(modules.lsmod(self.addr_space))
        mods = dict((self.addr_space.address_mask(mod.DllBase), mod) for mod in modlist)
        mod_addrs = sorted(mods.keys())

        drivers = dtree.DriverIrp(self._config).calculate()    

        for driver in drivers:
            owning_module = tasks.find_module(mods, mod_addrs, mods.values()[0].obj_vm.address_mask(driver.DriverStart))
            if owning_module:
                ret += [(str(owning_module.FullDllName), str(owning_module.BaseDllName), '0')]
            else:
                ret += [('UNKNOWN', 'UNKNOWN', '0')]

        return ret

    def get_file_object(self, filename):
        '''
        Gets file object corresponding to an executable image

        '''

        if filename:
            # Use same notation
            filename = self.normalize_filepath(filename)
            for f in self.files:
                # We consider they are the same file if executable path and file object path match
                try:
                    if re.match(r'^{0}$'.format(filename), f['name'], flags=re.IGNORECASE):
                        return self.extract_object(f)
                except TypeError:
                    pass

        return False, None

    def normalize_filepath(self, filepath):
        '''
        Converts filepath to use uniform notation

        '''

        to_replace = {
                        '\\SystemRoot': '\\\\Device\\\\HarddiskVolume[0-9]\\\\Windows',
                        '\\\\\\?\\C:': '\\\\Device\\\\HarddiskVolume[0-9]',
                        'C:': '\\\\Device\\\\HarddiskVolume[0-9]'
                    }

        for key in to_replace.keys():
            path = filepath.split(key)

            if len(path) == 2:
                return to_replace[key] + re.escape(path[1])

    def extract_object(self, file_object):
        '''
        Uses DumpFiles plugin to retrieve all FileObjects of memory dump

        '''

        try:
            # File fully memory resident
            if file_object['present'] and not file_object['pad']:
                self.count = self.count + 1
                return True, file_object
            elif file_object['present']:
                self.count1 = self.count1 + 1
                return False, file_object
        # SharedCacheMap has no 'present' attribute
        except KeyError:
            pass
        return False, None

    def validate_file(self, file_object):
        '''
        Validates signature thanks to ImageSectionObject and DataSectionObject attibutes of FileObjects

        @param file_object: FileOject dict

        @result string with verification process result
        '''

        # Read actual memory data
        content = self.read_file_memory(file_object)
        file_type = self.get_pe_type(file_object)

        # We need to undo executable relocation 
        if file_object['type'] == 'ImageSectionObject':
            try:
                return self.validate_image_section(content, file_type)
            except TypeError:
                return ReturnCode.FILEOBJECT_ERROR
        # Data is represented as on-disk, maybe with padding at the end
        elif file_object['type'] == 'DataSectionObject':
            return self.validate_data_section(content)

    def get_pe_type(self, file_object):
        return file_object['name'].split('.')[-1].lower()

    def read_file_memory(self, file_object):
        '''
        Reads all memory resident pages of a FileObject

        @param file_object: FileOject dict

        @returns str buffer with all FileObject content
        '''

        if file_object['type'] == "DataSectionObject":
            of = BytesIO()
            for mdata in file_object['present']:
                rdata = None
                if not mdata[0]:
                    continue
                try:
                    rdata = self.addr_space.base.read(mdata[0], mdata[2])
                except (IOError, OverflowError):
                    pass
                if not rdata:
                    continue
                of.seek(mdata[1])
                of.write(rdata)
                continue
            content = of.getvalue()
            of.close()
            return content
        elif file_object['type'] == "ImageSectionObject":
            of = BytesIO()
            for mdata in file_object['present']:
                rdata = None
                if not mdata[0]:
                    continue
                try:
                    rdata = self.addr_space.base.read(mdata[0], mdata[2])
                except (IOError, OverflowError):
                    pass
                if not rdata:
                    continue
                of.seek(mdata[1])
                of.write(rdata)
                continue
            content = of.getvalue()
            of.close()
            return content

    def validate_image_section(self, content, file_type):
        content = self.delete_padding(content)
        try:
            pe = pefile.PE(data=content, fast_load=True)
        except TypeError:
            return ReturnCode.PE_REBUILT_FAILED
        is_32bits = self.is_32bits(content)

        if pe.verify_checksum():
            return self.verify_pe(pe)

        for new_imagebase in self.frequent_addresses[file_type]:
            new_imagebase = int(new_imagebase, 16)

            if is_32bits and new_imagebase > 0xffffffff:
                continue

            try:
                pe = pefile.PE(data=content, fast_load=True)
                pe.relocate_image(new_imagebase)
                new_content = self.set_imagebase(new_imagebase, pe.__data__)
                pe = pefile.PE(data=new_content, fast_load=True)

                if pe.verify_checksum():
                    return self.verify_pe(pe)
            # AttributeError: Some PE files doesn't have relocation table
            # struct.error: Some times pe.get_data_from_qword() fails during relocation
            except (AttributeError, struct.error):
                pass

        return ReturnCode.PE_REBUILT_FAILED

    def verify_pe(self, pe):
        cert = self.sigv.extract_cert(pe)
        if cert:
            algorithm, hash_file = self.sigv.get_digest_from_signature(cert)    
            if algorithm:
                digest = self.sigv.calculate_pe_digest(algorithm, pe.__data__)    
                if hash_file == digest:
                    return self.sigv.verify_signature(cert)      
                else:
                    return ReturnCode.AUTHENTICODE_SIGNATURE_MISMATCH
            else:
                return ReturnCode.PARTIAL_CERTIFICATE
        else:
            for algorithm in ['md5', 'sha1', 'sha256']:
                digest = self.sigv.calculate_pe_digest(algorithm, pe.__data__)
                if self.sigv.is_in_catalog(digest):
                    return ReturnCode.CATALOG_SIGNED

            return ReturnCode.NOT_SIGNED

    def get_imagebase(self, content):
        nt_headers_addr = self.get_nt_header_addr(content)

        if self.is_32bits(content):
            return self.unpack_dword(content[nt_headers_addr+0x34:nt_headers_addr+0x34+0x4])
        elif self.is_64bits(content):
            return self.unpack_qword(content[nt_headers_addr+0x30:nt_headers_addr+0x30+0x8])

    def set_imagebase(self, imagebase, content):
        nt_headers_addr = self.get_nt_header_addr(content)

        if self.is_32bits(content):
            return content[:nt_headers_addr+0x34] + self.pack_dword(imagebase) + content[nt_headers_addr+0x34+0x4:]
        elif self.is_64bits(content):
            return content[:nt_headers_addr+0x30] + self.pack_qword(imagebase) + content[nt_headers_addr+0x30+0x8:]

    def is_32bits(self, content):
        nt_headers_addr = self.get_nt_header_addr(content)
            
        magic = content[nt_headers_addr+0x18:nt_headers_addr+0x18+0x2]

        return magic == b'\x0B\x01'

    def is_64bits(self, content):
        nt_headers_addr = self.get_nt_header_addr(content)
            
        magic = content[nt_headers_addr+0x18:nt_headers_addr+0x18+0x2]

        return magic == b'\x0B\x02'

    def get_pe_section(self, pe, section_name):
        for section in pe.sections:
            # Delete null-bytes at end, v.g: '.text\x00\x00'
            if section.Name.rstrip('\x00') == section_name:
                return section

    def validate_data_section(self, content):
        '''
        Validate signature of a DataSectionObject

        '''

        # Sometimes, there is padding at the end of buffer
        content = self.delete_padding(content)
        pe = pefile.PE(data=content, fast_load=True)

        # Ensure there are no extraction errors
        if pe.verify_checksum():
            # Files can has an embedded signature
            cert = self.sigv.extract_cert(pe)
            if cert:
                algorithm, hash_file = self.sigv.get_digest_from_signature(cert)
                digest = self.sigv.calculate_pe_digest(algorithm, content)
                if hash_file == digest:
                    return self.sigv.verify_signature(cert)
                else:
                    return ReturnCode.AUTHENTICODE_SIGNATURE_MISMATCH
            # Or files can has a signature in a separate catalog file
            else:
                # Calculate algorithm hash to later search in catalog files
                for algorithm in ['md5', 'sha1', 'sha256']:
                    digest = self.sigv.calculate_pe_digest(algorithm, content)
                    if self.sigv.is_in_catalog(digest):
                        return ReturnCode.CATALOG_SIGNED
                return ReturnCode.NOT_SIGNED
        else:
            return ReturnCode.PE_CHECKSUM_MISMATCH

    def get_nt_header_addr(self, pe_data):
        '''
        Gets NtHeader offset

        '''
        if pe_data[:2] == b'\x4D\x5A':              # MZ
            nt_headers_addr = self.unpack_dword(pe_data[0x3c:0x3c+0x04])
            nt_headers = pe_data[nt_headers_addr:nt_headers_addr+0x04]
            if nt_headers == b'\x50\x45\x00\x00':   # PE
                return nt_headers_addr

    def unpack_dword(self, bytes_):
        return struct.unpack('<I', bytes_)[0]

    def pack_dword(self, bytes_):
        return struct.pack('<I', bytes_)

    def unpack_qword(self, bytes_):
        return struct.unpack('<Q', bytes_)[0]

    def pack_qword(self, bytes_):
        return struct.pack('<Q', bytes_)

    def delete_padding(self, content):
        '''
        Deletes padding of SectionObject containing an executable

        '''

        real_size = self.calculate_pe_size(content)
        content = content[:real_size]


        return content

    def calculate_pe_size(self, data):
        '''
        Calculate the size of an executable adding size of PE headers, all sections,
        and Authenticode signature

        '''

        # Assume PE file is well formed
        try:
            pe = pefile.PE(data=data, fast_load=True)
        except pefile.PEFormatError:
            return ReturnCode.FILEOBJECT_ERROR
        # PE Headers
        size = pe.NT_HEADERS.OPTIONAL_HEADER.SizeOfHeaders
        # All sections
        for section in pe.sections:
            size += section.SizeOfRawData
        # Authenticode signature, if any
        size += pe.OPTIONAL_HEADER.DATA_DIRECTORY[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_SECURITY']].Size

        return size

    def validate_partial_file(self, file_object):
        if file_object:
            content = self.read_file_memory(file_object)
            try:
                pe = pefile.PE(data=content, fast_load=True)
                if self.sigv.has_cert(pe):
                    if file_object['type'] == 'DataSectionObject':
                        cert = self.sigv.extract_cert(pe)
                        if cert:
                            return '{0:s}. Signature verification: {1}'.format(ReturnCode.PARTIAL_CONTENT_VERIFIED, self.sigv.verify_signature(cert))
                        else:
                            return ReturnCode.CONTENT_SIGNED_NOT_VERIFIED
                    # SecurityDirectory entry is not mappped into memory in ImageSectionObject
                    elif file_object['type'] == 'ImageSectionObject':
                        return ReturnCode.CONTENT_SIGNED_NOT_VERIFIED
                else:
                    # Microsoft programs in 'C:\Windows' are usually catalog-signed
                    if re.match(r'\Device\HarddiskVolume[0-9]\Windows', file_object['name']):
                        return ReturnCode.PARTIAL_CONTENT_MAYBE_CATALOG_SIGNED

                    return ReturnCode.PARTIAL_CONTENT_NOT_SIGNED
            except pefile.PEFormatError:
                return ReturnCode.PARTIAL_CONTENT_PE_DATA_ERROR
        else:
            return ReturnCode.FILEOBJECT_ERROR

    def __debug_message(self, type_, message):
        getattr(debug, type_)('{0}\t: {1}'.format(self.__plugin_name, message))

    def unified_output(self, data):
        return TreeGrid([
                            ('Module', str),
                            ('Pid', int),
                            ('Result', str)
                        ],
                        self.generator(data))

    def generator(self, data):
        for process_name, pid, result in data:
            yield (0, [str(process_name), int(pid), str(result)])

    def render_text(self, outfd, data):
        self.table_header(outfd,
                              [('Module', '25s'),
                               ('Pid', '>6'),
                               ('Result', '120s')]
                         )

        for process_name, pid, result in data:
            self.table_row(outfd,
                           process_name,
                           pid,
                           result
                          )
        outfd.write("complete PE file counts:{0}".format(self.count))
        outfd.write("uncomplete PE file counts:{0}".format(self.count1))
