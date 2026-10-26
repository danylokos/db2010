"""Bounded MAIN polled-I2C protocol; unknown slave registers remain unknown.

Register roles/commands come from original MAIN, not a silicon datasheet.
Completion latency and slave presence are explicit experimental inputs.
"""


class I2CFault(Exception):
    def __init__(self, reason, **details):
        self.reason,self.details=reason,details
        super().__init__(reason)


class PolledI2C:
    def __init__(self, config):
        required={'profile','address','completion_instructions','power_inputs'}
        if (not required<=set(config)<=required|{'capture_boot_writes'}
                or config['profile']!='marita-polled-byte' or config['address']!=0xF9040000
                or type(config['completion_instructions']) is not int
                or not 1<=config['completion_instructions']<=16):
            raise ValueError('Requires explicit polled I2C profile and bounded instruction latency')
        inputs=config['power_inputs']
        if inputs is not None and (set(inputs)!={'usb_connected','charger_connected'}
                or any(type(v) is not bool for v in inputs.values())):
            raise ValueError('Power inputs must be explicit booleans or unknown')
        if type(config.get('capture_boot_writes',False)) is not bool:
            raise ValueError('Boot-write capture requires an explicit boolean')
        self.config=config
        self.base=config['address']
        self.control,self.clock,self.staged,self.received=None,None,None,None
        self.phase='uninitialized'
        self.pending=None
        self.slave,self.register=None,None
        self.wire=[]
        self.transactions=[]
        self.captured_writes=[]

    def contains_page(self,address,size):
        return self.base<address+size and address<self.base+0x1000

    def finish(self,now):
        if self.pending is not None and now>=self.pending['due']:
            self.control &= ~self.pending['mask']
            if self.pending['operation'] in ('transmit','receive'):
                self.control |= 8
            if self.pending['operation']=='stop':
                self.phase='idle'
                self.transactions.append(dict(slave=self.slave,register=self.register,
                                              wire=list(self.wire),received=self.received))
            self.pending=None

    def begin(self,operation,mask,now):
        self.pending=dict(operation=operation,mask=mask,
                          due=now+self.config['completion_instructions'])
        self.control |= mask

    def power_read(self):
        if self.slave!=0x4A:
            raise I2CFault('i2c_slave_unmodeled',slave=self.slave)
        inputs=self.config['power_inputs']
        if self.register!=0x23 or inputs is None:
            raise I2CFault('i2c_slave_register_unmodeled',slave=self.slave,register=self.register)
        # MAIN's StartupVoltageCheck interprets only these two presence bits.
        # Other bits are zero in this explicit scenario, not measured reset data.
        return (0x80 if inputs['usb_connected'] else 0)|(8 if inputs['charger_connected'] else 0)

    def access(self,kind,address,size,now,value=None):
        if size!=2 or address not in (self.base,self.base+2,self.base+4):
            raise I2CFault('i2c_register_access_unmodeled',address=address,size=size)
        self.finish(now)
        if kind=='read':
            if address==self.base+2:
                if self.control is None:
                    raise I2CFault('i2c_initial_state_unknown',address=address)
                return self.control
            if address==self.base+4:
                if self.clock is None:
                    raise I2CFault('i2c_initial_state_unknown',address=address)
                return self.clock
            if self.phase!='received' or self.pending is not None or self.received is None:
                raise I2CFault('i2c_data_read_unmodeled',phase=self.phase)
            self.phase='read-complete'
            return self.received
        if kind!='write' or type(value) is not int or not 0<=value<=0xFFFF:
            raise ValueError('Invalid I2C bus operation')
        if address==self.base+4:
            if value!=0x60:
                raise I2CFault('i2c_clock_value_unmodeled',value=value)
            self.clock=value
            return value
        if address==self.base:
            if value>255 or self.staged is not None or self.pending is not None:
                raise I2CFault('i2c_data_write_unmodeled',value=value,phase=self.phase)
            self.staged=value
            return value
        if value==4:
            self.control=4;self.phase='configured'
            self.pending=None;self.staged=None;self.received=None
            self.slave=None;self.register=None;self.wire=[]
            return value
        if self.control is None or self.clock is None:
            raise I2CFault('i2c_initial_state_unknown',address=address)
        if self.pending is not None:
            raise I2CFault('i2c_command_while_busy',value=value)
        if value==0x24:
            if self.staged is not None or self.phase not in ('idle','register-selected'):
                raise I2CFault('i2c_start_sequence_unmodeled',phase=self.phase)
            self.phase='write-address' if self.phase=='idle' else 'read-address'
            self.control=4
            self.begin('start',0x20,now)
            return value
        if value==0x10:
            if self.phase not in ('read-complete','write-complete'):
                raise I2CFault('i2c_stop_sequence_unmodeled',phase=self.phase)
            self.control=0
            self.begin('stop',0x10,now)
            return value
        if value!=0:
            raise I2CFault('i2c_control_value_unmodeled',value=value)
        if self.phase=='configured' and self.staged==0:
            self.staged=None;self.phase='idle';self.control=0
            return value
        byte=self.staged
        if self.phase=='write-address' and byte is not None:
            if byte!=0x94:
                raise I2CFault('i2c_slave_unmodeled',slave=byte>>1,address_byte=byte)
            self.slave=byte>>1;self.phase='register'
        elif self.phase=='register' and byte is not None:
            self.register=byte;self.phase='register-selected'
        elif self.phase=='read-address' and byte is not None:
            if byte!=0x95 or byte>>1!=self.slave:
                raise I2CFault('i2c_read_address_unmodeled',address_byte=byte,slave=self.slave)
            self.phase='receive-ready'
        elif self.phase=='receive-ready' and byte is None:
            received=self.power_read()
            self.received=received;self.phase='received';self.control=0
            self.begin('receive',0,now)
            return value
        elif self.phase=='register-selected' and byte is not None:
            if (not self.config.get('capture_boot_writes',False) or self.slave!=0x4A
                    or (self.register,byte) not in ((0xB0,0),(0xB1,0xAA))):
                raise I2CFault('i2c_slave_register_write_unmodeled',slave=self.slave,
                               register=self.register,value=byte)
            self.captured_writes.append(dict(slave=self.slave,register=self.register,value=byte))
            self.phase='write-complete'
        else:
            raise I2CFault('i2c_transfer_sequence_unmodeled',phase=self.phase,byte=byte)
        self.wire.append(byte);self.staged=None;self.control=0
        self.begin('transmit',0,now)
        return value

    def summary(self):
        return dict(control=self.control,clock=self.clock,phase=self.phase,
                    pending=self.pending,staged=self.staged,received=self.received,
                    slave=self.slave,register=self.register,wire=list(self.wire),
                    transactions=list(self.transactions),
                    **({'captured_writes':list(self.captured_writes)} if self.config.get('capture_boot_writes',False) else {}))
