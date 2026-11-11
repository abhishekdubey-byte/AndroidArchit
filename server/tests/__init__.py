from app.stdio_utf8 import force_utf8_stdio

force_utf8_stdio()  # services print '→' etc.; a cp1252 Windows console must not crash the tests
