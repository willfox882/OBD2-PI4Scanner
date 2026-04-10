import logging
import os

def setup_logging(debug: bool = False, log_file: str = "obd_debug.log"):
    """
    Sets up logging to a file. 
    CRITICAL: We do not log to stdout/stderr because it will corrupt the curses UI.
    """
    level = logging.DEBUG if debug else logging.INFO
    
    # Remove all handlers associated with the root logger object.
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)
        
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file)
        ]
    )

def get_logger(name: str):
    return logging.getLogger(name)
