"""Bounded x8 NAND reset/ID/status and optional small-page spare reads.

Samsung K9F56XXX0B revision1.1 documents the chip commands and EC/35 IDs.
DB2010 byte ports and read gating are inferred from original firmware.
Immediate readiness and WP# high are explicit model assumptions.
"""


import hashlib


class NandFault(Exception):
    def __init__(self,reason,**details):
        self.reason,self.details=reason,details
        super().__init__(reason)


class Nand:
    def __init__(self,config):
        base_profile=dict(profile='samsung-256m-x8-reset-id-status',address=0x50000000,
                          manufacturer_id=0xEC,device_id=0x35,completion='immediate-ready',
                          write_protected=False)
        self.spare_enabled='spare_read' in config
        self.program_enabled='spare_program' in config
        expected=dict(base_profile)
        if self.spare_enabled:
            expected['spare_read']=dict(profile='samsung-x8-read2-unknown-contents')
        if self.program_enabled:
            if not self.spare_enabled:raise ValueError('Spare programming requires Read2')
            expected['spare_program']=dict(profile='samsung-x8-known-spare-byte-program')
        if 'synthetic_spare' in config:
            if not self.program_enabled:raise ValueError('Synthetic spare fixture requires both protocols')
            expected['synthetic_spare']=dict(profile='first-erase-block-ff-valid-footer')
        if config.get('write_protected') is not False or config != expected:
            raise ValueError('NAND requires the explicit EC/35 x8 reset/ID/status profile')
        self.config=dict(config);self.base=config['address']
        self.mode='unknown';self.read_open=False;self.id_address=None;self.id_cursor=0
        self.reset_count=0
        self.spare_addresses=[];self.spare_page=None;self.spare_column=None
        self.spare_bytes={}
        self.program_buffer=[];self.program_counts={};self.program_operations=[]
        if 'synthetic_spare' in config:
            for page in range(32):
                raw=bytearray(b'\xff'*16)
                if page==0:raw[14:16]=b'\x54\x12'
                self.seed_spare(page,0,bytes(raw))

    def seed_spare(self,page,column,data):
        """Sparse, explicit fixture contents; production boot supplies none."""
        if (not self.spare_enabled or type(page) is not int or not 0<=page<65536
                or type(column) is not int or not 0<=column<16
                or not isinstance(data,bytes) or not data or column+len(data)>16):
            raise ValueError('Invalid bounded spare seed')
        if any((page,column+i) in self.spare_bytes for i in range(len(data))):
            raise ValueError('Spare seed overlaps known bytes')
        self.spare_bytes.update({(page,column+i):b for i,b in enumerate(data)})

    def contains_page(self,address,size):
        return address<self.base+0x1000 and self.base<address+size

    def access(self,kind,address,size,value=None):
        off=address-self.base
        if size!=1 or off not in (0,8,0x10,0x20,0x24):
            raise NandFault('nand_access_unmodeled',address=address,size=size,kind=kind)
        if kind=='write':
            if type(value) is not int or not 0<=value<=255:
                raise NandFault('nand_write_value_unmodeled',address=address,value=value)
            if off in (0x20,0x24):
                if value!=0:raise NandFault('nand_gate_value_unmodeled',address=address,value=value)
                self.read_open=off==0x20
            elif off==0x10:
                if self.mode in ('spare-program-address','spare-program-data') and value not in (0xFF,0x10):
                    raise NandFault('nand_program_pending_unmodeled',address=address,command=value,mode=self.mode)
                if value==0xFF:
                    self.reset_count+=1;self.mode='idle';self.id_address=None;self.id_cursor=0
                    self.read_open=False
                    self.spare_addresses=[];self.spare_page=None;self.spare_column=None
                    self.program_buffer=[]
                elif value in (0x90,0x70):
                    if not self.reset_count:
                        raise NandFault('nand_handoff_state_unmodeled',address=address,value=value)
                    if value==0x70 and self.mode=='spare-address':
                        raise NandFault('nand_incomplete_spare_address',address=address,cycles=len(self.spare_addresses))
                    self.mode='id-address' if value==0x90 else 'status'
                    if value==0x90:
                        self.id_address=None;self.id_cursor=0
                        self.spare_addresses=[];self.spare_page=None;self.spare_column=None
                elif value==0x50 and self.spare_enabled:
                    if not self.reset_count:
                        raise NandFault('nand_handoff_state_unmodeled',address=address,value=value)
                    if self.mode=='status' and self.spare_page is not None:
                        self.mode='spare-read'
                    else:
                        self.mode='spare-address';self.spare_addresses=[]
                        self.spare_page=None;self.spare_column=None
                elif value==0x80 and self.program_enabled:
                    if self.mode!='spare-address' or self.spare_addresses:
                        raise NandFault('nand_program_pointer_unmodeled',address=address,mode=self.mode)
                    self.mode='spare-program-address';self.program_buffer=[]
                elif value==0x10 and self.program_enabled:
                    if self.mode!='spare-program-data' or not self.program_buffer:
                        raise NandFault('nand_program_confirm_unmodeled',address=address,mode=self.mode)
                    page,column=self.spare_page,self.spare_column
                    if self.program_counts.get(page,0)>=3:
                        raise NandFault('nand_spare_partial_program_limit',address=address,page=page)
                    previous=[]
                    for i,b in enumerate(self.program_buffer):
                        key=(page,column+i)
                        if key not in self.spare_bytes:
                            raise NandFault('nand_program_unknown_spare',address=address,page=page,column=column+i)
                        old=self.spare_bytes[key]
                        if old&b!=b:
                            raise NandFault('nand_program_requires_erase',address=address,page=page,column=column+i)
                        previous.append(old)
                    for i,b in enumerate(self.program_buffer):self.spare_bytes[(page,column+i)]=b
                    self.program_counts[page]=self.program_counts.get(page,0)+1
                    self.program_operations.append(dict(page=page,column=column,old_hex=bytes(previous).hex(),
                                                        new_hex=bytes(self.program_buffer).hex()))
                    self.program_buffer=[];self.mode='program-complete'
                    self.spare_addresses=[];self.spare_page=None;self.spare_column=None
                elif value in (0,1,0x50,0x80,0x10,0x60,0xD0,0x8A):
                    raise NandFault('nand_array_operation_unmodeled',address=address,command=value,mode=self.mode)
                else:
                    raise NandFault('nand_command_unmodeled',address=address,command=value)
            elif off==8:
                if self.mode in ('spare-address','spare-program-address'):
                    programming=self.mode=='spare-program-address'
                    self.spare_addresses.append(value)
                    if len(self.spare_addresses)==3:
                        column,low,high=self.spare_addresses
                        self.spare_page=low|(high<<8)
                        self.spare_column=column&15  # Samsung x8 ignores A4..A7 in Read2.
                        self.mode='spare-program-data' if programming else 'spare-read'
                elif self.mode!='id-address' or value!=0:
                    raise NandFault('nand_address_sequence_unmodeled',address=address,value=value,mode=self.mode)
                else:
                    self.id_address=0;self.id_cursor=0;self.mode='id'
            elif off==0 and self.mode=='spare-program-data':
                if self.spare_column+len(self.program_buffer)>=16:
                    raise NandFault('nand_program_spare_bounds',address=address,page=self.spare_page)
                self.program_buffer.append(value)
            else:
                raise NandFault('nand_data_write_unmodeled',address=address,value=value)
            return value
        if kind!='read' or off!=0:
            raise NandFault('nand_register_read_unmodeled',address=address,mode=self.mode)
        if not self.read_open:
            raise NandFault('nand_data_gate_closed',address=address,mode=self.mode)
        if self.mode=='id':
            if self.id_cursor>=2:
                raise NandFault('nand_id_read_unmodeled',address=address,index=self.id_cursor)
            result=(self.config['manufacturer_id'],self.config['device_id'])[self.id_cursor]
            self.id_cursor+=1
            return result
        if self.mode=='status':
            return 0xC0
        if self.mode=='spare-address':
            raise NandFault('nand_incomplete_spare_address',address=address,cycles=len(self.spare_addresses))
        if self.mode=='spare-read':
            if self.spare_column>=16:
                raise NandFault('nand_spare_sequential_page_unmodeled',address=address,page=self.spare_page,
                                column=self.spare_column)
            key=(self.spare_page,self.spare_column)
            if key not in self.spare_bytes:
                raise NandFault('nand_read_unknown_spare',address=address,page=self.spare_page,
                                column=self.spare_column,absolute_column=512+self.spare_column)
            result=self.spare_bytes[key];self.spare_column+=1
            return result
        raise NandFault('nand_data_read_unmodeled',address=address,mode=self.mode)

    def summary(self):
        result=dict(config=self.config,mode=self.mode,read_open=self.read_open,
                    id_address=self.id_address,id_cursor=self.id_cursor,reset_count=self.reset_count)
        if self.spare_enabled:
            result['spare_read']=dict(address_cycles=list(self.spare_addresses),page=self.spare_page,
                column=self.spare_column,known_bytes=len(self.spare_bytes),
                seeds=[dict(page=p,column=c,value=b) for (p,c),b in sorted(self.spare_bytes.items())])
        if 'synthetic_spare' in self.config:
            # The frozen generator/configuration and program log preserve the
            # bytes; a sparse-state hash avoids repeating 512 entries per bus cycle.
            result['spare_read'].pop('seeds')
            raw=b''.join(p.to_bytes(2,'little')+bytes([c,b]) for (p,c),b in sorted(self.spare_bytes.items()))
            result['spare_read']['known_contents_sha256']=hashlib.sha256(raw).hexdigest()
        if self.program_enabled:
            result['spare_program']=dict(buffer=list(self.program_buffer),
                page_program_counts={str(p):n for p,n in sorted(self.program_counts.items())},
                operations=list(self.program_operations),completion='immediate-ready',
                initial_partial_program_counts=0)
        return result
