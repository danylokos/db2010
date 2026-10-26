"""Explicit steady UART RX pad inputs on GPIO port1; no synthesized RX traffic."""


class SerialLineFault(Exception):
    def __init__(self,reason,**details):
        self.reason,self.details=reason,details
        super().__init__(reason)


class SerialLines:
    DATA=0xF900000A
    DIRECTION=0xF900000C

    def __init__(self,config):
        if (set(config)!={'profile','uart0_rx_level','uart1_rx_level'}
                or config['profile']!='port1-rx-levels'
                or any(type(config[key]) is not int or config[key] not in (0,1)
                       for key in ('uart0_rx_level','uart1_rx_level'))):
            raise ValueError('Serial inputs require explicit steady GPIO port1 RX levels')
        self.config=dict(config)
        self.level_bits=config['uart0_rx_level']|(config['uart1_rx_level']<<4)

    def sample(self,latched,direction):
        if direction is None:
            raise SerialLineFault('serial_gpio_direction_unknown',address=self.DIRECTION)
        # Original pin configuration clears direction for RX, sets it for TX.
        # Input-mode bits sample external levels; output-mode bits retain latch.
        input_mask=0x11&~direction
        value=(latched&~input_mask)|(self.level_bits&input_mask)
        return dict(value=value,latched=latched,direction=direction,input_mask=input_mask,
                    level_bits=self.level_bits,source='explicit-steady-rx-pad-inputs')
