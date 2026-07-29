# ============================================================================
# onvif_ptz.py - Controle PTZ via ONVIF
#
# Detecta automaticamente se a câmera reporta pan/tilt normalizados (-1..1)
# ou já em graus (ex: -180..180 / -90..90), consultando GetConfigurationOptions.
# Isso evita "chutar" a conversão para graus.
# ============================================================================
import threading
import time
from onvif import ONVIFCamera


class PTZController:
    def __init__(self, ip, port, user, password):
        self._lock = threading.Lock()
        self.cam = ONVIFCamera(ip, port, user, password)
        self.media = self.cam.create_media_service()
        self.ptz = self.cam.create_ptz_service()

        profiles = self.media.GetProfiles()
        self.profile = profiles[0]
        self.profile_token = self.profile.token

        self._load_ranges()

    # -------------------------------------------------------------------
    def _load_ranges(self):
        req = self.ptz.create_type('GetConfigurationOptions')
        req.ConfigurationToken = self.profile.PTZConfiguration.token
        options = self.ptz.GetConfigurationOptions(req)

        pan_space = options.Spaces.AbsolutePanTiltPositionSpace[0]
        zoom_space = options.Spaces.AbsoluteZoomPositionSpace[0]

        self.pan_min = float(pan_space.XRange.Min)
        self.pan_max = float(pan_space.XRange.Max)
        self.tilt_min = float(pan_space.YRange.Min)
        self.tilt_max = float(pan_space.YRange.Max)
        self.zoom_min = float(zoom_space.XRange.Min)
        self.zoom_max = float(zoom_space.XRange.Max)

        # Se a faixa reportada é ~[-1, 1], é espaço normalizado -> converte p/ graus
        # assumindo pan mecânico de até 180/-180 e tilt até 90/-90 (ajuste se sua
        # câmera tiver outro curso mecânico real, ex: pan contínuo 0-360).
        self.pan_normalized = abs(self.pan_max) <= 1.5
        self.tilt_normalized = abs(self.tilt_max) <= 1.5
        self.pan_deg_range = 180.0
        self.tilt_deg_range = 90.0

    # -------------------------------------------------------------------
    def _raw_to_deg(self, raw_pan, raw_tilt):
        pan_deg = raw_pan * self.pan_deg_range if self.pan_normalized else raw_pan
        tilt_deg = raw_tilt * self.tilt_deg_range if self.tilt_normalized else raw_tilt
        return pan_deg, tilt_deg

    def _deg_to_raw_pan(self, pan_deg):
        raw = pan_deg / self.pan_deg_range if self.pan_normalized else pan_deg
        return max(self.pan_min, min(self.pan_max, raw))

    def _deg_to_raw_tilt(self, tilt_deg):
        raw = tilt_deg / self.tilt_deg_range if self.tilt_normalized else tilt_deg
        return max(self.tilt_min, min(self.tilt_max, raw))

    def _zoom_raw_to_pct(self, raw_zoom):
        span = (self.zoom_max - self.zoom_min) or 1.0
        return round((raw_zoom - self.zoom_min) / span * 100.0, 2)

    def _zoom_pct_to_raw(self, pct):
        pct = max(0.0, min(100.0, pct))
        return self.zoom_min + (pct / 100.0) * (self.zoom_max - self.zoom_min)

    # -------------------------------------------------------------------
    def go_home(self):
        """Move para o ponto zero das coordenadas ONVIF (pan=0, tilt=0, zoom mínimo)."""
        with self._lock:
            req = self.ptz.create_type('AbsoluteMove')
            req.ProfileToken = self.profile_token
            req.Position = {
                'PanTilt': {'x': self._deg_to_raw_pan(0.0), 'y': self._deg_to_raw_tilt(0.0)},
                'Zoom': {'x': self.zoom_min},
            }
            self.ptz.AbsoluteMove(req)

    def get_status(self):
        """Retorna (pan_deg, tilt_deg, zoom_pct) atuais."""
        with self._lock:
            req = self.ptz.create_type('GetStatus')
            req.ProfileToken = self.profile_token
            status = self.ptz.GetStatus(req)
            raw_pan = float(status.Position.PanTilt.x)
            raw_tilt = float(status.Position.PanTilt.y)
            raw_zoom = float(status.Position.Zoom.x)
        pan_deg, tilt_deg = self._raw_to_deg(raw_pan, raw_tilt)
        zoom_pct = self._zoom_raw_to_pct(raw_zoom)
        return pan_deg, tilt_deg, zoom_pct

    def move_absolute(self, pan_deg, tilt_deg, zoom_pct):
        with self._lock:
            req = self.ptz.create_type('AbsoluteMove')
            req.ProfileToken = self.profile_token
            req.Position = {
                'PanTilt': {'x': self._deg_to_raw_pan(pan_deg), 'y': self._deg_to_raw_tilt(tilt_deg)},
                'Zoom': {'x': self._zoom_pct_to_raw(zoom_pct)},
            }
            self.ptz.AbsoluteMove(req)

    def move_relative(self, pan_delta_deg=0.0, tilt_delta_deg=0.0, zoom_delta_pct=0.0):
        pan_deg, tilt_deg, zoom_pct = self.get_status()
        new_pan = max(-self.pan_deg_range, min(self.pan_deg_range, pan_deg + pan_delta_deg)) \
            if self.pan_normalized else pan_deg + pan_delta_deg
        new_tilt = max(-self.tilt_deg_range, min(self.tilt_deg_range, tilt_deg + tilt_delta_deg)) \
            if self.tilt_normalized else tilt_deg + tilt_delta_deg
        new_zoom = max(0.0, min(100.0, zoom_pct + zoom_delta_pct))
        self.move_absolute(new_pan, new_tilt, new_zoom)
        return new_pan, new_tilt, new_zoom
