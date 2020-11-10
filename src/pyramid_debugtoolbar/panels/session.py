from pyramid.interfaces import ISessionFactory
import zope.interface.interfaces

from pyramid_debugtoolbar.panels import DebugPanel
from pyramid_debugtoolbar.utils import dictrepr

_ = lambda x: x


class NotInSession(object):
    pass


class SessionDebugPanel(DebugPanel):
    """
    A panel to display Pyramid's ``ISession`` data.
    """

    name = 'session'
    template = 'pyramid_debugtoolbar.panels:templates/session.dbtmako'
    title = _('Session')
    nav_title = title
    user_activate = True

    @property
    def has_content(self):
        """
        This is too difficult to figure out under the following parameters:
        * do not trigger the ``ISession`` interface
        * The toolbar consults this attibute relatively early in the lifecycle
          to determine if ``.is_active`` should be ``True``
        """
        return True

    # used to store the Request for processing
    _request = None

    def __init__(self, request):
        """
        initial setup of the `data` payload
        """
        self.data = {
            "configuration": None,
            "is_active": None,  # not known on `.__init__`
            "NotInSession": NotInSession,
            "session_accessed": {
                "pre": None,  # pre-processing (toolbar)
                "panel_setup": None,  # during the panel setup?
                "main": None,  # during Request processing
                "post": None,  # post-processing (tooolbar)
            },
            "session_data": {
                "ingress": {},  # in
                "egress": {},  # out
                "keys": set([]),
                "changed": set([]),
            },
        }
        # we need this for processing in the response phase
        self._request = request
        # try to stash the configuration info
        try:
            config = request.registry.getUtility(ISessionFactory)
            self.data["configuration"] = config
        except zope.interface.interfaces.ComponentLookupError:
            # the `ISessionFactory` is not configured
            pass

    def wrap_handler(self, handler):
        """
        ``wrap_handler`` allows us to monitor the entire lifecycle of
        the  ``Request``.

        Instead of using this hook to create a new wrapped handler, we can just
        do the required analysis right here, and then invoke the original
        handler.

        Request | "ingress"
        Pre-process the ``Request`` if the panel is active, or if the
        ``Session`` has already been accessed, as the ``Request`` requires
        activating the ``Session`` interface.
        If pre-processing does not happen, the ``.session`` property will be
        replaced with a wrapped function which will invoke the ingress
        processing if the session is accessed.
        """
        _data = self.data

        if self.is_active:
            # not known on `.__init__` due to the toolbar's design.
            # no problem, it can be updated on `.wrap_handler`
            _data["is_active"] = True

        if "session" in self._request.__dict__:
            # mark the ``Session`` as already accessed.
            # This can happen in two situations:
            #   * The panel is activated by the user for extended logging
            #   * The ``Session`` was accessed by another panel or higher tween
            _data["session_accessed"]["pre"] = True

        if self.is_active or ("session" in self._request.__dict__):
            """
            This block handles two situations:
            * The panel is activated by the user for extended logging
            * The ``Session`` was accessed by another panel or higher tween

            This uses a two-phased analysis, because we may trigger a generic
            ``AttributeError`` when accessing the ``Session`` if no
            ``ISessionFactory`` was configured.
            """
            session = None
            try:
                session = self._request.session
                if not _data["session_accessed"]["pre"]:
                    _data["session_accessed"]["panel_setup"] = True
            except AttributeError:
                # the ``ISession`` interface is not configured
                pass
            if session is not None:
                for k, v in dictrepr(session):
                    _data["session_data"]["ingress"][k] = v
                    _data["session_data"]["keys"].add(k)

                if "session" in self._request.__dict__:
                    # Delete the loaded ``.session`` from the ``Request``;
                    # it will be replaced with the wrapper function below.
                    # note: This approach preserves the already-loaded
                    #       ``Session``, we are just wrapping it within
                    #       a function.
                    del self._request.__dict__["session"]

                # If the ``Session`` was not already loaded, then we may have
                # just loaded it. This presents a problem for tracking, as we
                # will not know if the ``Session`` was accessed or not.
                # To handle this scenario we use a variant of the ``wrap_load``
                # function from the ``request_vars`` tolbar:
                def _session_wrapper(self):
                    # This function updates the ``self.data`` information dict,
                    # and then returns the exact same ``Session`` we just
                    # deleted from the ``Request``.
                    _data["session_accessed"]["main"] = True
                    return session

                # Replace the existing ``ISession`` interface with our wrapper.
                self._request.set_property(
                    _session_wrapper, name="session", reify=True
                )

        else:
            """
            This block handles the default situation:
            * The ``Session`` has not been accessed and the Panel is not enabled
            """
            orig_property = getattr(self._request.__class__, "session", None)
            if orig_property is not None:
                # We have a ``Session`` but it has not been accessed yet.
                # The ``.session`` attribute is replaced with a variant of the
                # ``wrap_load`` from the ``request_vars`` tolbar.
                # The wrapper updates out information dict about how the
                # ``Session`` was accessed and notes the ingress values.

                def wrapper(self):
                    _session = orig_property.__get__(self)
                    # note the session was accessed during the main request
                    _data["session_accessed"]["main"] = True
                    # process the inbound session data
                    if not _data["session_data"]["ingress"]:
                        for k, v in dictrepr(_session):
                            _data["session_data"]["ingress"][k] = v
                            _data["session_data"]["keys"].add(k)
                    # Replace the existing ``ISession`` interface with
                    # our wrapper.
                    return _session

                self._request.set_property(wrapper, name="session", reify=True)

        return handler

    def process_response(self, response):
        """
        ``Response`` | "egress"

        Only process the ``Response``` if the panel is active OR if the
        session was accessed, as processing the ``Response`` requires
        opening the session.
        """
        if self._request is None:
            # this scenario can happen if there is an error in the toolbar
            return

        _data = self.data

        if self.is_active or ("session" in self._request.__dict__):
            try:
                if "session" not in self._request.__dict__:
                    # the ``Session`` is not already loaded, so we should
                    # mark it as being loaded within the "post" phase.
                    _data["session_accessed"]["post"] = True
                # if we installed a wrapped load, accessing the session now
                # will trigger the "main" marker. to handle this, check the
                # current version of the marker then access the session
                # and then reset the marker
                _accessed_main = _data["session_accessed"]["main"]
                _session = self._request.session
                _data["session_accessed"]["main"] = _accessed_main
                for k, v in dictrepr(_session):
                    _data["session_data"]["egress"][k] = v
                    _data["session_data"]["keys"].add(k)
                    if _data["session_accessed"]["panel_setup"]:
                        # we can not detect `changed` values unless we process
                        # the ``Session`` during the "pre" hook.
                        if (k not in _data["session_data"]["ingress"]) or (
                            _data["session_data"]["ingress"][k] != v
                        ):
                            _data["session_data"]["changed"].add(k)
            except AttributeError:
                # the session is not configured
                pass


def includeme(config):
    config.add_debugtoolbar_panel(SessionDebugPanel)
