"""Native DirectShow (IAMCameraControl/IAMVideoProcAmp) brightness/focus
control via comtypes, used by `CameraSession` in preference to OpenCV's
unreliable `VideoCapture.set()`. See the header comment below for why."""

from typing import Dict


# --- Optional native Windows camera control -------------------------------
#
# OpenCV's `VideoCapture.set()` on a live MSMF/DSHOW stream is unreliable for
# brightness/focus on many UVC drivers, and manual focus gets silently
# overridden while autofocus is on. DirectShow's IAMVideoProcAmp (brightness)
# and IAMCameraControl (focus) talk to the driver directly and are what
# native "camera settings" utilities use. There is no public typelib for
# these two interfaces (they predate typelib-based automation), so they are
# declared by hand via `comtypes` rather than generated — this also avoids
# comtypes.client's runtime typelib codegen/caching, which would be fragile
# in a frozen PyInstaller build. Everything here is best-effort: any failure
# (comtypes missing, device not matched, COM error) falls back to the
# existing OpenCV-based property path in `CameraSession`.
try:
    import ctypes as _ctypes
    from ctypes import HRESULT as _HRESULT, POINTER as _POINTER, c_long as _c_long, c_ulong as _c_ulong, c_ulonglong as _c_ulonglong, c_int as _c_int
    import comtypes
    from comtypes import GUID, COMMETHOD, IUnknown, IPersist, CoCreateInstance
    from comtypes.persist import IPropertyBag

    _DSHOW_CONTROL_AVAILABLE = True
except Exception:
    comtypes = None
    _DSHOW_CONTROL_AVAILABLE = False

if _DSHOW_CONTROL_AVAILABLE:
    _CLSID_SYSTEM_DEVICE_ENUM = GUID("{62BE5D10-60EB-11d0-BD3B-00A0C911CE86}")
    _CLSID_VIDEO_INPUT_DEVICE_CATEGORY = GUID("{860BB310-5D01-11d0-BD3B-00A0C911CE86}")
    _IID_ICREATE_DEV_ENUM = GUID("{29840822-5B84-11D0-BD3B-00A0C911CE86}")

    DSHOW_CAMERA_CONTROL_EXPOSURE = 4
    DSHOW_CAMERA_CONTROL_FOCUS = 6
    DSHOW_CAMERA_CONTROL_FLAGS_AUTO = 0x0001
    DSHOW_CAMERA_CONTROL_FLAGS_MANUAL = 0x0002

    class _IBaseFilter(IUnknown):
        # Only ever used as a QueryInterface waypoint to IAMVideoProcAmp /
        # IAMCameraControl; its own methods are intentionally not declared
        # since nothing here calls them.
        _iid_ = GUID("{56A86895-0AD4-11CE-B03A-0020AF0BA770}")

    class _IEnumMoniker(IUnknown):
        _iid_ = GUID("{00000102-0000-0000-C000-000000000046}")
        _methods_ = [
            COMMETHOD([], _HRESULT, "Next",
                      (["in"], _c_ulong, "celt"),
                      (["out"], _POINTER(_POINTER(IUnknown)), "rgelt"),
                      (["out"], _POINTER(_c_ulong), "pceltFetched")),
        ]

    class _IPersistStream(IPersist):
        # IMoniker really derives IUnknown -> IPersist -> IPersistStream ->
        # IMoniker. Skipping these two intermediate interfaces shifts every
        # IMoniker vtable slot below (BindToObject/BindToStorage would land
        # on IPersist::GetClassID's slot instead), so they must be declared
        # even though nothing here calls them directly.
        _iid_ = GUID("{00000109-0000-0000-C000-000000000046}")
        _methods_ = [
            COMMETHOD([], _HRESULT, "IsDirty"),
            COMMETHOD([], _HRESULT, "Load", (["in"], _POINTER(IUnknown), "pstm")),
            COMMETHOD([], _HRESULT, "Save", (["in"], _POINTER(IUnknown), "pstm"), (["in"], _c_int, "fClearDirty")),
            COMMETHOD([], _HRESULT, "GetSizeMax", (["out"], _POINTER(_c_ulonglong), "pcbSize")),
        ]

    class _IMoniker(_IPersistStream):
        _iid_ = GUID("{0000000f-0000-0000-C000-000000000046}")
        _methods_ = [
            COMMETHOD([], _HRESULT, "BindToObject",
                      (["in"], _POINTER(IUnknown), "pbc"),
                      (["in"], _POINTER(IUnknown), "pmkToLeft"),
                      (["in"], _POINTER(GUID), "riid"),
                      (["out"], _POINTER(_POINTER(IUnknown)), "ppvResult")),
            COMMETHOD([], _HRESULT, "BindToStorage",
                      (["in"], _POINTER(IUnknown), "pbc"),
                      (["in"], _POINTER(IUnknown), "pmkToLeft"),
                      (["in"], _POINTER(GUID), "riid"),
                      (["out"], _POINTER(_POINTER(IUnknown)), "ppvObj")),
        ]

    class _ICreateDevEnum(IUnknown):
        _iid_ = _IID_ICREATE_DEV_ENUM
        _methods_ = [
            COMMETHOD([], _HRESULT, "CreateClassEnumerator",
                      (["in"], _POINTER(GUID), "clsidDeviceClass"),
                      (["out"], _POINTER(_POINTER(_IEnumMoniker)), "ppEnumMoniker"),
                      (["in"], _c_ulong, "dwFlags")),
        ]

    class _IAMCameraControl(IUnknown):
        _iid_ = GUID("{C6E13370-30AC-11d0-A18C-00A0C9118956}")
        _methods_ = [
            COMMETHOD([], _HRESULT, "GetRange",
                      (["in"], _c_long, "Property"),
                      (["out"], _POINTER(_c_long), "pMin"),
                      (["out"], _POINTER(_c_long), "pMax"),
                      (["out"], _POINTER(_c_long), "pSteppingDelta"),
                      (["out"], _POINTER(_c_long), "pDefault"),
                      (["out"], _POINTER(_c_long), "pCapsFlags")),
            COMMETHOD([], _HRESULT, "Set",
                      (["in"], _c_long, "Property"), (["in"], _c_long, "lValue"), (["in"], _c_long, "Flags")),
            COMMETHOD([], _HRESULT, "Get",
                      (["in"], _c_long, "Property"), (["out"], _POINTER(_c_long), "lValue"), (["out"], _POINTER(_c_long), "Flags")),
        ]

    # CAMERA_PROP_SPECS name -> (COM interface, property id, manual flag, auto flag)
    #
    # "brightness" is deliberately mapped to IAMCameraControl::Exposure, not
    # IAMVideoProcAmp::Brightness. Measured on real hardware (Microsoft LifeCam
    # Cinema): Set()/Get() on VideoProcAmp Brightness round-trips fine (the
    # driver echoes back whatever value you write) but never changes a single
    # captured pixel — mean frame brightness stayed ~25-28 across the full
    # 30-255 range. GetRange() also reported capsFlags=0 (neither Auto nor
    # Manual advertised), consistent with the property being an unwired
    # legacy shim on this driver. CameraControl Exposure, by contrast, swung
    # mean frame brightness from ~9 (min) to ~229 (max) - a real, dramatic
    # effect - so it is used as the actual "brightness" control instead. This
    # is a known quirk on many UVC webcams, not specific to one camera model.
    _DSHOW_PROPERTY_MAP = {
        "brightness": (_IAMCameraControl, DSHOW_CAMERA_CONTROL_EXPOSURE, DSHOW_CAMERA_CONTROL_FLAGS_MANUAL, DSHOW_CAMERA_CONTROL_FLAGS_AUTO),
        "focus": (_IAMCameraControl, DSHOW_CAMERA_CONTROL_FOCUS, DSHOW_CAMERA_CONTROL_FLAGS_MANUAL, DSHOW_CAMERA_CONTROL_FLAGS_AUTO),
    }

    def _dshow_create_bind_ctx():
        ptr = _ctypes.c_void_p()
        hr = _ctypes.windll.ole32.CreateBindCtx(0, _ctypes.byref(ptr))
        if hr != 0 or not ptr:
            raise OSError(f"CreateBindCtx failed hr={hr}")
        return _ctypes.cast(ptr, _POINTER(IUnknown))

    def _dshow_moniker_name(moniker, bind_ctx) -> str:
        raw = moniker.BindToStorage(bind_ctx, None, IPropertyBag._iid_)
        return raw.QueryInterface(IPropertyBag).Read("FriendlyName", pErrorLog=None)

    def find_dshow_video_filter(descriptor_friendly_name: str, fallback_index: int):
        """Enumerate DirectShow video-input monikers and bind the one matching
        `descriptor_friendly_name` (preferred) or `fallback_index`
        (positional fallback, since MSMF/DSHOW enumeration order can differ
        from the app's own device_index in rare cases) to IBaseFilter.
        Caller's thread must have called `comtypes.CoInitialize()` first."""
        bind_ctx = _dshow_create_bind_ctx()
        dev_enum = CoCreateInstance(_CLSID_SYSTEM_DEVICE_ENUM, interface=_ICreateDevEnum)
        enum_moniker = dev_enum.CreateClassEnumerator(_CLSID_VIDEO_INPUT_DEVICE_CATEGORY, 0)
        if not enum_moniker:
            return None
        candidates = []
        while True:
            moniker, fetched = enum_moniker.Next(1)
            if fetched == 0 or moniker is None:
                break
            moniker = moniker.QueryInterface(_IMoniker)
            try:
                name = _dshow_moniker_name(moniker, bind_ctx)
            except Exception:
                name = ""
            candidates.append((moniker, name))
        target_moniker = None
        if descriptor_friendly_name:
            for moniker, name in candidates:
                if name and name.strip().lower() == descriptor_friendly_name.strip().lower():
                    target_moniker = moniker
                    break
        if target_moniker is None and 0 <= fallback_index < len(candidates):
            target_moniker = candidates[fallback_index][0]
        if target_moniker is None:
            return None
        return target_moniker.BindToObject(bind_ctx, None, _IBaseFilter._iid_).QueryInterface(_IBaseFilter)

    class DirectShowPropertyController:
        """Per-device wrapper around IAMCameraControl / IAMVideoProcAmp.

        Must only be used from the thread that created it (COM STA rules) -
        in `CameraSession` that is always the preview thread."""

        def __init__(self, base_filter):
            self._base_filter = base_filter
            self._interfaces: Dict[type, object] = {}

        def _interface_for(self, name: str):
            interface_cls = _DSHOW_PROPERTY_MAP[name][0]
            if interface_cls not in self._interfaces:
                self._interfaces[interface_cls] = self._base_filter.QueryInterface(interface_cls)
            return self._interfaces[interface_cls]

        def get_range(self, name: str):
            prop_id = _DSHOW_PROPERTY_MAP[name][1]
            return self._interface_for(name).GetRange(prop_id)

        def set_manual(self, name: str, value: int) -> float:
            _, prop_id, manual_flag, _ = _DSHOW_PROPERTY_MAP[name]
            iface = self._interface_for(name)
            iface.Set(prop_id, int(value), manual_flag)
            reported, _flags = iface.Get(prop_id)
            return float(reported)

        def set_auto(self, name: str) -> None:
            _, prop_id, _, auto_flag = _DSHOW_PROPERTY_MAP[name]
            iface = self._interface_for(name)
            current, _flags = iface.Get(prop_id)
            iface.Set(prop_id, current, auto_flag)
