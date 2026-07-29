# ============================================================================
# onvif_ptz.py - Controle PTZ via ONVIF (versão responsiva)
#
# Mudanças em relação à versão anterior:
#   • Suporte a ContinuousMove/Stop (movimento fluido enquanto o botão está
#     pressionado) - é o que resolve a "dificuldade de movimentar".
#   • Suporte a RelativeMove (passo único sem precisar de um GetStatus antes).
#   • Cache da última posição lida, para não gastar uma viagem ONVIF extra
#     em cada comando.
#   • Detecta automaticamente quais espaços (absoluto/relativo/contínuo) a
#     câmera realmente anuncia, em vez de assumir.
# ============================================================================
import threading
import time

from onvif import ONVIFCamera


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


class PTZController:
    def __init__(self, ip, port, user, password, label="ptz",
                 pan_deg_range=180.0, tilt_deg_range=90.0):
        self.label = label
        self._lock = threading.Lock()

        self.cam = ONVIFCamera(ip, port, user, password)
        self.media = self.cam.create_media_service()
        self.ptz = self.cam.create_ptz_service()

        profiles = self.media.GetProfiles()
        self.profile = profiles[0]
        self.profile_token = self.profile.token

        self.pan_deg_range = float(pan_deg_range)
        self.tilt_deg_range = float(tilt_deg_range)

        self._cache = (0.0, 0.0, 0.0)
        self._cache_time = 0.0

        self._load_ranges()

    # -------------------------------------------------------------------
    @staticmethod
    def _first(container, name):
        """Pega o primeiro elemento de um espaço ONVIF opcional, ou None."""
        try:
            value = getattr(container, name, None)
        except Exception:
            return None
        if not value:
            return None
        try:
            return value[0]
        except (TypeError, IndexError):
            return value

    @staticmethod
    def _range(space, axis="XRange"):
        if space is None:
            return None
        try:
            rng = getattr(space, axis)
            return float(rng.Min), float(rng.Max)
        except Exception:
            return None

    # -------------------------------------------------------------------
    def _load_ranges(self):
        req = self.ptz.create_type('GetConfigurationOptions')
        req.ConfigurationToken = self.profile.PTZConfiguration.token
        options = self.ptz.GetConfigurationOptions(req)
        spaces = options.Spaces

        abs_pt = self._first(spaces, 'AbsolutePanTiltPositionSpace')
        abs_zoom = self._first(spaces, 'AbsoluteZoomPositionSpace')
        rel_pt = self._first(spaces, 'RelativePanTiltTranslationSpace')
        rel_zoom = self._first(spaces, 'RelativeZoomTranslationSpace')
        cont_pt = self._first(spaces, 'ContinuousPanTiltVelocitySpace')
        cont_zoom = self._first(spaces, 'ContinuousZoomVelocitySpace')

        # --- absoluto (obrigatório para o "home" e para o zoom por retângulo)
        pan_r = self._range(abs_pt, "XRange") or (-1.0, 1.0)
        tilt_r = self._range(abs_pt, "YRange") or (-1.0, 1.0)
        zoom_r = self._range(abs_zoom, "XRange") or (0.0, 1.0)

        self.pan_min, self.pan_max = pan_r
        self.tilt_min, self.tilt_max = tilt_r
        self.zoom_min, self.zoom_max = zoom_r

        self.has_absolute = abs_pt is not None

        # Faixa reportada ~[-1,1] => espaço normalizado, converte para graus
        self.pan_normalized = abs(self.pan_max) <= 1.5
        self.tilt_normalized = abs(self.tilt_max) <= 1.5

        # --- relativo
        self.has_relative = rel_pt is not None
        rel_pan_r = self._range(rel_pt, "XRange") or (-1.0, 1.0)
        rel_tilt_r = self._range(rel_pt, "YRange") or (-1.0, 1.0)
        self.rel_pan_min, self.rel_pan_max = rel_pan_r
        self.rel_tilt_min, self.rel_tilt_max = rel_tilt_r
        self.has_relative_zoom = rel_zoom is not None
        rel_zoom_r = self._range(rel_zoom, "XRange") or (-1.0, 1.0)
        self.rel_zoom_min, self.rel_zoom_max = rel_zoom_r

        # --- contínuo (o que dá a resposta imediata nos botões)
        self.has_continuous = cont_pt is not None
        cont_pan_r = self._range(cont_pt, "XRange") or (-1.0, 1.0)
        cont_tilt_r = self._range(cont_pt, "YRange") or (-1.0, 1.0)
        self.cont_pan_min, self.cont_pan_max = cont_pan_r
        self.cont_tilt_min, self.cont_tilt_max = cont_tilt_r
        self.has_continuous_zoom = cont_zoom is not None
        cont_zoom_r = self._range(cont_zoom, "XRange") or (-1.0, 1.0)
        self.cont_zoom_min, self.cont_zoom_max = cont_zoom_r

    def describe(self):
        return (
            f"[{self.label}] pan=[{self.pan_min},{self.pan_max}] norm={self.pan_normalized} | "
            f"tilt=[{self.tilt_min},{self.tilt_max}] norm={self.tilt_normalized} | "
            f"zoom=[{self.zoom_min},{self.zoom_max}]\n"
            f"[{self.label}] suporta: absoluto={self.has_absolute} "
            f"relativo={self.has_relative}(zoom={self.has_relative_zoom}) "
            f"continuo={self.has_continuous}(zoom={self.has_continuous_zoom})"
        )

    # -------------------------------------------------------------------
    # Conversões
    # -------------------------------------------------------------------
    def _raw_to_deg(self, raw_pan, raw_tilt):
        pan_deg = raw_pan * self.pan_deg_range if self.pan_normalized else raw_pan
        tilt_deg = raw_tilt * self.tilt_deg_range if self.tilt_normalized else raw_tilt
        return pan_deg, tilt_deg

    def _deg_to_raw_pan(self, pan_deg):
        raw = pan_deg / self.pan_deg_range if self.pan_normalized else pan_deg
        return _clamp(raw, self.pan_min, self.pan_max)

    def _deg_to_raw_tilt(self, tilt_deg):
        raw = tilt_deg / self.tilt_deg_range if self.tilt_normalized else tilt_deg
        return _clamp(raw, self.tilt_min, self.tilt_max)

    def _zoom_raw_to_pct(self, raw_zoom):
        span = (self.zoom_max - self.zoom_min) or 1.0
        return round((raw_zoom - self.zoom_min) / span * 100.0, 2)

    def _zoom_pct_to_raw(self, pct):
        pct = _clamp(pct, 0.0, 100.0)
        return self.zoom_min + (pct / 100.0) * (self.zoom_max - self.zoom_min)

    # -------------------------------------------------------------------
    # Leitura
    # -------------------------------------------------------------------
    def get_status(self, max_age=0.0):
        """Retorna (pan_deg, tilt_deg, zoom_pct). Se max_age > 0 e o cache for
        mais novo que isso, devolve o cache sem falar com a câmera."""
        if max_age > 0.0 and (time.time() - self._cache_time) < max_age:
            return self._cache

        with self._lock:
            req = self.ptz.create_type('GetStatus')
            req.ProfileToken = self.profile_token
            status = self.ptz.GetStatus(req)
            raw_pan = float(status.Position.PanTilt.x)
            raw_tilt = float(status.Position.PanTilt.y)
            raw_zoom = float(status.Position.Zoom.x)

        pan_deg, tilt_deg = self._raw_to_deg(raw_pan, raw_tilt)
        zoom_pct = self._zoom_raw_to_pct(raw_zoom)
        self._cache = (pan_deg, tilt_deg, zoom_pct)
        self._cache_time = time.time()
        return self._cache

    # -------------------------------------------------------------------
    # Movimento contínuo (botão pressionado)
    # -------------------------------------------------------------------
    def move_continuous(self, pan_speed=0.0, tilt_speed=0.0, zoom_speed=0.0):
        """Velocidades normalizadas em -1..1. Move até receber stop()."""
        if not self.has_continuous:
            raise RuntimeError("Câmera não anuncia ContinuousPanTiltVelocitySpace")

        velocity = {}
        if pan_speed or tilt_speed:
            velocity['PanTilt'] = {
                'x': _clamp(pan_speed, self.cont_pan_min, self.cont_pan_max),
                'y': _clamp(tilt_speed, self.cont_tilt_min, self.cont_tilt_max),
            }
        if zoom_speed and self.has_continuous_zoom:
            velocity['Zoom'] = {
                'x': _clamp(zoom_speed, self.cont_zoom_min, self.cont_zoom_max)
            }

        if not velocity:
            return False

        with self._lock:
            req = self.ptz.create_type('ContinuousMove')
            req.ProfileToken = self.profile_token
            req.Velocity = velocity
            self.ptz.ContinuousMove(req)
        return True

    def stop(self, pan_tilt=True, zoom=True):
        with self._lock:
            req = self.ptz.create_type('Stop')
            req.ProfileToken = self.profile_token
            req.PanTilt = pan_tilt
            req.Zoom = zoom
            self.ptz.Stop(req)
        # invalida o cache: a posição mudou durante o movimento
        self._cache_time = 0.0

    # -------------------------------------------------------------------
    # Movimento por passo
    # -------------------------------------------------------------------
    def move_relative(self, pan_delta_deg=0.0, tilt_delta_deg=0.0, zoom_delta_pct=0.0):
        """Passo único. Usa RelativeMove quando disponível (1 viagem ONVIF);
        senão cai no AbsoluteMove usando a posição em cache."""
        if self.has_relative:
            translation = {}
            if pan_delta_deg or tilt_delta_deg:
                tx = pan_delta_deg / self.pan_deg_range if self.pan_normalized else pan_delta_deg
                ty = tilt_delta_deg / self.tilt_deg_range if self.tilt_normalized else tilt_delta_deg
                translation['PanTilt'] = {
                    'x': _clamp(tx, self.rel_pan_min, self.rel_pan_max),
                    'y': _clamp(ty, self.rel_tilt_min, self.rel_tilt_max),
                }
            if zoom_delta_pct and self.has_relative_zoom:
                tz = zoom_delta_pct / 100.0
                translation['Zoom'] = {
                    'x': _clamp(tz, self.rel_zoom_min, self.rel_zoom_max)
                }
            if translation:
                with self._lock:
                    req = self.ptz.create_type('RelativeMove')
                    req.ProfileToken = self.profile_token
                    req.Translation = translation
                    self.ptz.RelativeMove(req)
                self._cache_time = 0.0
            # estimativa otimista da nova posição (o loop de telemetria corrige)
            pan, tilt, zoom = self._cache
            return (pan + pan_delta_deg, tilt + tilt_delta_deg,
                    _clamp(zoom + zoom_delta_pct, 0.0, 100.0))

        # fallback: absoluto a partir do cache (evita o GetStatus extra)
        pan_deg, tilt_deg, zoom_pct = self.get_status(max_age=0.5)
        new_pan = pan_deg + pan_delta_deg
        new_tilt = tilt_deg + tilt_delta_deg
        if self.pan_normalized:
            new_pan = _clamp(new_pan, -self.pan_deg_range, self.pan_deg_range)
        if self.tilt_normalized:
            new_tilt = _clamp(new_tilt, -self.tilt_deg_range, self.tilt_deg_range)
        new_zoom = _clamp(zoom_pct + zoom_delta_pct, 0.0, 100.0)
        self.move_absolute(new_pan, new_tilt, new_zoom)
        return new_pan, new_tilt, new_zoom

    def move_absolute(self, pan_deg, tilt_deg, zoom_pct):
        with self._lock:
            req = self.ptz.create_type('AbsoluteMove')
            req.ProfileToken = self.profile_token
            req.Position = {
                'PanTilt': {
                    'x': self._deg_to_raw_pan(pan_deg),
                    'y': self._deg_to_raw_tilt(tilt_deg),
                },
                'Zoom': {'x': self._zoom_pct_to_raw(zoom_pct)},
            }
            self.ptz.AbsoluteMove(req)
        self._cache = (pan_deg, tilt_deg, _clamp(zoom_pct, 0.0, 100.0))
        self._cache_time = time.time()
        return self._cache

    def go_home(self):
        """Ponto zero das coordenadas ONVIF (pan=0, tilt=0, zoom mínimo)."""
        return self.move_absolute(0.0, 0.0, 0.0)