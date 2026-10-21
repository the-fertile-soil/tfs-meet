-- TFS: start the server-side (Jibri) recording automatically when the meeting owner joins a room.
-- Options (conf.d/00-tfs-limits.cfg.lua):
--   tfs_autorecord_enabled (default true)
--   tfs_autorecord_delay_seconds (default 6)
local st = require "util.stanza";
local jid_split = require "util.jid".split;

local ENABLED = module:get_option_boolean("tfs_autorecord_enabled", true);
local DELAY = module:get_option_number("tfs_autorecord_delay_seconds", 6);
local MAIN_DOMAIN = module:get_option_string("muc_mapper_domain_base", "meet.jitsi");
local FOCUS = "focus@auth." .. MAIN_DOMAIN .. "/focus";

local muc_service = prosody.hosts[module.host].modules.muc;

local function on_joined(event)
	if not ENABLED then return; end
	local room, occupant = event.room, event.occupant;
	if room._tfs_autorecord then return; end
	if string.find(room.jid, "breakout", 1, true) then return; end
	local bare = occupant.bare_jid;
	local _, host = jid_split(bare);
	if host ~= MAIN_DOMAIN and host ~= ("guest." .. MAIN_DOMAIN) then return; end
	module:log("info", "TFS autorecord: first user %s joined %s", bare, room.jid);

	room._tfs_autorecord = true;
	module:log("info", "TFS autorecord: owner %s joined %s, starting recording in %d s", bare, room.jid, DELAY);
	module:add_timer(DELAY, function()
		if muc_service.get_room_from_jid(room.jid) ~= room then return; end
		local iq = st.iq({ type = "set", from = occupant.nick, to = FOCUS, id = "tfs-rec-" .. tostring(os.time()) });
		iq:tag("jibri", {
			xmlns = "http://jitsi.org/protocol/jibri",
			action = "start",
			recording_mode = "file",
			app_data = '{"file_recording_metadata":{"share":false}}',
		});
		module:log("info", "TFS autorecord: sending start for %s", room.jid); module:send(iq);
		module:log("info", "TFS autorecord: start request sent for %s", room.jid);
	end);
end

module:hook("muc-occupant-joined", on_joined, -1);
