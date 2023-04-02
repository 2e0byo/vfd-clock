from machine import Pin

D = tuple(Pin(x, Pin.OUT) for x in (21, 19, 18, 5, 17, 16, 4, 2))

digits = tuple(Pin(x, Pin.OUT) for x in (32, 33, 25, 26, 27, 14))
