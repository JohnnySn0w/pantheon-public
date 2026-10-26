class NetcapsBreach(Exception):
    def __init__(self, reason: str, event: str, detail: str = ""):
        self.reason = reason
        self.event = event
        self.detail = detail
