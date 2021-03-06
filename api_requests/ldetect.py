import time

from api import fieldtypes
from api.web import RainwaveHandler
from api.server import handle_api_url
from api.server import handle_url
from api.exceptions import APIException

from libs import cache
from libs import log
from libs import db
from rainwave import user
from backend import sync_to_front

# Sample Icecast query:
# &server=myserver.com&port=8000&client=1&mount=/live&user=&pass=&ip=127.0.0.1&agent="My%20player"

# TODO: we need to separate tune in records by station ID maybe?

class IcecastHandler(RainwaveHandler):
	auth_required = False
	sid_required = False
	description = "Accessible only to relays for the purpose of tracking listeners."

	def prepare(self):
		self.failed = True    # Assume failure unless otherwise
		self.relay = fieldtypes.valid_relay(self.request.remote_ip)

		if not self.relay:
			self.set_status(403)
			self.append("%s is not a valid relay." % self.request.remote_ip)
			self.finish()
			return

		super(IcecastHandler, self).prepare()

	def finish(self, chunk = None):
		if self.failed:
			self.set_status(403)
			self.set_header("icecast-auth-user", "0")
		else:
			self.set_status(200)
			self.set_header("icecast-auth-user", "1")
		super(IcecastHandler, self).finish()

	def write_error(self, status_code, **kwargs):
		self.failed = True
		if kwargs.has_key("exc_info"):
			exc = kwargs['exc_info'][1]
			if isinstance(exc, APIException):
				exc.localize(self.locale)
				self.set_header("icecast-auth-message", exc.reason)
		super(IcecastHandler, self).finish()

	def append(self, message, dct = None):
		log.debug("ldetect", message)
		self.set_header("icecast-auth-message", message)
		self.write(message)

@handle_api_url("listener_add/(\d+)")
class AddListener(IcecastHandler):
	fields = {
		"client": (fieldtypes.integer, True),
		"mount": (fieldtypes.icecast_mount, True),
		"ip": (fieldtypes.ip_address, True),
		"agent": (fieldtypes.media_player, True)
	}

	# local testing only
	# allow_get = True
	# def get(self, sid):
	# 	self.post(sid)

	def post(self, sid):
		(self.mount, self.user_id, self.listen_key) = self.get_argument("mount")
		self.agent = self.get_argument("agent")
		self.listener_ip = self.get_argument("ip")

		# if self.mount in config.station_mounts:
		# 	self.sid = config.station_mounts[self.mount]
		if sid:
			try:
				self.sid = int(sid)
			except ValueError:
				raise APIException("invalid_station_id", http_code=400)
		else:
			raise APIException("invalid_station_id", http_code=400)
		if self.user_id > 1:
			self.add_registered(self.sid)
		else:
			self.add_anonymous(self.sid)

	def add_registered(self, sid):
		real_key = db.c.fetch_var("SELECT radio_listenkey FROM phpbb_users WHERE user_id = %s", (self.user_id,))
		if real_key != self.listen_key:
			raise APIException("invalid_argument", reason="mismatched listen_key.")
		tunedin = db.c.fetch_var("SELECT COUNT(*) FROM r4_listeners WHERE user_id = %s", (self.user_id,))
		if tunedin:
			db.c.update(
				"UPDATE r4_listeners "
				"SET sid = %s, listener_ip = %s, listener_purge = FALSE, listener_icecast_id = %s, listener_relay = %s, listener_agent = %s "
				"WHERE user_id = %s",
				(sid, self.get_argument("ip"), self.get_argument("client"), self.relay, self.agent, self.user_id))
			self.append("Registered user %s record updated to be tuned in to %s." % (self.user_id, sid))
			self.failed = False
		else:
			db.c.update("INSERT INTO r4_listeners "
				"(sid, user_id, listener_ip, listener_icecast_id, listener_relay, listener_agent) "
				"VALUES (%s, %s, %s, %s, %s, %s)",
				(sid, self.user_id, self.get_argument("ip"), self.get_argument("client"), self.relay, self.agent))
			self.append("Registered user %s is now tuned in." % self.user_id)
			self.failed = False
		if not self.failed:
			u = user.User(self.user_id)
			u.get_listener_record(use_cache=False)
			if u.has_requests():
				u.put_in_request_line(sid)
		sync_to_front.sync_frontend_user_id(self.user_id)

	def add_anonymous(self, sid):
		# Here we'll erase any extra records for the same IP address (shouldn't happen but you never know, especially
		# if the system gets a reset).  There is a small flaw here; there's a chance we'll pull in 2 clients with the same client ID.
		# I (rmcauley) am classifying this as "collatoral damage" - an anon user who is actively using the website
		# can re-tune-in on the small chance that this occurs.
		records = db.c.fetch_list("SELECT listener_icecast_id FROM r4_listeners WHERE listener_ip = %s", (self.get_argument("ip"),))
		if len(records) == 0:
			db.c.update("INSERT INTO r4_listeners "
					"(sid, listener_ip, user_id, listener_relay, listener_agent, listener_icecast_id) "
					"VALUES (%s, %s, %s, %s, %s, %s)",
				(sid, self.get_argument("ip"), 1, self.relay, self.get_argument("agent"), self.get_argument("client")))
			sync_to_front.sync_frontend_ip(self.get_argument("ip"))
			self.append("Anonymous user from IP %s is now tuned in with record." % self.get_argument("ip"))
			self.failed = False
		else:
			# Keep one valid entry on file for the listener by popping once
			records.pop()
			# Erase the rest
			while len(records) > 1:
				db.c.update("DELETE FROM r4_listeners WHERE listener_icecast_id = %s", (records.pop(),))
				log.debug("ldetect", "Deleted extra record for icecast ID %s from IP %s." % (self.get_argument("client"), self.get_argument("ip")))
			db.c.update("UPDATE r4_listeners SET listener_icecast_id = %s, listener_purge = FALSE WHERE listener_ip = %s", (self.get_argument("client"), self.get_argument("ip")))
			self.append("Anonymous user from IP %s record updated." % self.get_argument("ip"))
			self.failed = False
		sync_to_front.sync_frontend_ip(self.get_argument("ip"))

@handle_api_url("listener_remove")
class RemoveListener(IcecastHandler):
	fields = {
		"client": (fieldtypes.integer, True),
	}

	# local testing only
	# allow_get = True
	# def get(self, sid):
	# 	self.post(sid)

	def post(self, sid):
		listener = db.c.fetch_row("SELECT user_id, listener_ip FROM r4_listeners WHERE listener_relay = %s AND listener_icecast_id = %s",
								 (self.relay, self.get_argument("client")))
		if not listener:
			return self.append("No user record to delete for client %s on relay %s." % (self.get_argument("client"), self.relay))

		db.c.update("UPDATE r4_listeners SET listener_purge = TRUE WHERE listener_relay = %s AND listener_icecast_id = %s", (self.relay, self.get_argument("client")))
		if listener['user_id'] > 1:
			self.append("Registered user ID %s flagged for removal." % (listener['user_id'],))
			db.c.update("UPDATE r4_request_line SET line_expiry_tune_in = %s WHERE user_id = %s", (time.time() + 600, listener['user_id']))
			cache.set_user(listener['user_id'], "listener_record", None)
			sync_to_front.sync_frontend_user_id(listener['user_id'])
		else:
			self.append("Anonymous user, client ID %s relay %s flagged for removal." % (self.get_argument("client"), self.relay))
			sync_to_front.sync_frontend_ip(listener['listener_ip'])
		self.failed = False

# Compatible with R4 beta relay
@handle_api_url("listener_remove/(\d+)")
class RemoveListener_ForR4Beta(RemoveListener):
	pass

# Compatible with R3 relays
@handle_url("/sync/(\d+)/listener_add")
class AddListener_R3Relay(AddListener):
	pass

# Compatible with R3 relays
@handle_url("/sync/(\d+)/listener_remove")
class RemoveListener_R3Relay(RemoveListener):
	pass