"""PL080 initialization profile: real clear semantics, disabled channels and strict transfer boundary."""


class DmaFault(Exception):
    def __init__(self,reason,**details):
        self.reason,self.details=reason,details
        super().__init__(reason)


class Pl080:
    CHANNEL_FIELDS={0:'source',4:'destination',8:'lli',12:'control',16:'configuration'}
    GLOBAL_FIELDS={0x20:'soft_burst',0x24:'soft_single',0x28:'soft_last_burst',
                   0x2C:'soft_last_single',0x30:'configuration',0x34:'sync'}

    def __init__(self,config):
        if config!={'profile':'cold-disabled-init','address':0xF2000000,'channels':8}:
            raise ValueError('PL080 initialization requires explicit eight-channel cold-disabled profile')
        self.config=dict(config)
        self.address=config['address']
        self.global_words={name:0 for name in self.GLOBAL_FIELDS.values()}
        self.channels=[{name:0 for name in self.CHANNEL_FIELDS.values()} for _ in range(8)]
        self.raw_tc,self.raw_error=0,0

    def contains(self,address,size):
        return address<self.address+0x1000 and address+size>self.address

    def summary(self):
        return dict(config=self.config,global_words=dict(self.global_words),raw_tc=self.raw_tc,
                    raw_error=self.raw_error,channels=[dict(c) for c in self.channels])

    def access(self,kind,address,size,value=None):
        off=address-self.address
        if size!=4 or off%4:
            raise DmaFault('dma_access_width_unmodeled',address=address,size=size)
        channel,field=None,None
        if 0x100<=off<0x200:
            channel=(off-0x100)//0x20
            field=self.CHANNEL_FIELDS.get((off-0x100)%0x20)
        if field is not None:
            state=self.channels[channel]
            name=f'channel-{channel}-{field}'
            if kind=='read':return dict(register=name,value=state[field])
            value&=0xFFFFFFFF
            if field=='configuration':
                if value&~0x5FFFF:
                    raise DmaFault('dma_channel_configuration_unmodeled',address=address,size=size,value=value)
                if value&1:
                    raise DmaFault('dma_channel_execution_unmodeled',address=address,size=size,value=value,
                                   channel=channel,descriptor=dict(state))
            if field=='lli' and value&2:
                raise DmaFault('dma_lli_reserved_bit_unmodeled',address=address,size=size,value=value)
            if field=='control' and (((value>>18)&7)>2 or ((value>>21)&7)>2):
                raise DmaFault('dma_control_width_unmodeled',address=address,size=size,value=value)
            old=state[field];state[field]=value
            return dict(register=name,value=value,before=old,after=value)
        if off in self.GLOBAL_FIELDS:
            name=self.GLOBAL_FIELDS[off]
            if kind=='read':return dict(register=name,value=self.global_words[name])
            value&=0xFFFFFFFF
            if value&~(7 if off==0x30 else 0xFFFF):
                raise DmaFault('dma_global_bits_unmodeled',address=address,size=size,value=value)
            if off in (0x20,0x24,0x28,0x2C) and value:
                raise DmaFault('dma_software_request_unmodeled',address=address,size=size,value=value)
            old=self.global_words[name];self.global_words[name]=value
            return dict(register=name,value=value,before=old,after=value)
        if off in (8,16):
            if kind=='read':raise DmaFault('dma_clear_read_unmodeled',address=address,size=size)
            if value&~255:raise DmaFault('dma_clear_bits_unmodeled',address=address,size=size,value=value)
            name='raw_tc' if off==8 else 'raw_error'
            old=getattr(self,name);new=old&~value;setattr(self,name,new)
            return dict(register=name+'-clear',value=value,before=old,after=new)
        if off in (0,4,12,20,24,28):
            if kind!='read':raise DmaFault('dma_status_write_unmodeled',address=address,size=size,value=value)
            tc_mask=sum(1<<i for i,c in enumerate(self.channels) if c['configuration']&0x8000)
            error_mask=sum(1<<i for i,c in enumerate(self.channels) if c['configuration']&0x4000)
            tc,error=self.raw_tc&tc_mask,self.raw_error&error_mask
            status={0:('interrupt-status',tc|error),4:('terminal-count-status',tc),12:('error-status',error),
                    20:('raw-terminal-count-status',self.raw_tc),24:('raw-error-status',self.raw_error),
                    28:('enabled-channels',sum(1<<i for i,c in enumerate(self.channels) if c['configuration']&1))}
            name,value=status[off]
            return dict(register=name,value=value)
        raise DmaFault('dma_register_unmodeled',address=address,size=size)
