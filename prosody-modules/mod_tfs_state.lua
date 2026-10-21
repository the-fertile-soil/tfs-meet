-- TFS: write a small JSON file listing active rooms so the web app can show availability.
local muc_service = prosody.hosts[module.host].modules.muc;
local OUT = "/config/tfs-rooms.json";

local function write_state()
	local ok, err = pcall(function()
		local parts = {};
		local rooms = muc_service.live_rooms and muc_service.live_rooms() or muc_service.each_room();
		for room in rooms do
			local name = room.jid:match("^([^@]+)@");
			local n = 0;
			for _, occ in room:each_occupant() do
				local host = occ.bare_jid and occ.bare_jid:match("@(.+)$") or "";
				if host == "meet.jitsi" or host == "guest.meet.jitsi" then n = n + 1; end
			end
			if name and n > 0 then
				room._tfs_started = room._tfs_started or os.time();
				parts[#parts + 1] = string.format('{"room":"%s","occupants":%d,"started":%d}', name, n, room._tfs_started);
			end
		end
		local f = io.open(OUT .. ".tmp", "w");
		if f then
			f:write('{"updated":' .. os.time() .. ',"rooms":[' .. table.concat(parts, ",") .. ']}');
			f:close();
			os.rename(OUT .. ".tmp", OUT);
		end
	end);
	if not ok then module:log("warn", "tfs_state write failed: %s", tostring(err)); end
end

local function later() module:add_timer(1, function() write_state(); end); end
module:hook("muc-occupant-joined", later, -2);
module:hook("muc-occupant-left", later, -2);
module:hook("muc-room-destroyed", later, -2);
module:add_timer(5, function() write_state(); return 20; end);

-- MMN: attendance log (name, email, join/leave) for the post-meeting report.
local PRES = "/config/mmn-presence.jsonl";
local function jesc(s) return (tostring(s or ""):gsub("[%c\"\\]", " ")); end
local function plog(ev, room, occ, origin, stanza)
	local okp, errp = pcall(function()
		local host = occ.bare_jid and occ.bare_jid:match("@(.+)$") or "";
		if host ~= "meet.jitsi" and host ~= "guest.meet.jitsi" then return; end
		room._mmn_who = room._mmn_who or {};
		local key = occ.bare_jid;
		if ev == "join" then
			local nick = stanza and stanza:get_child_text("nick", "http://jabber.org/protocol/nick") or "";
			local u = origin and origin.jitsi_meet_context_user or {};
			room._mmn_who[key] = { name = u.name or nick, email = u.email or "" };
		end
		local w = room._mmn_who[key] or { name = "", email = "" };
		local f = io.open(PRES, "a");
		if f then
			f:write(string.format('{"t":%d,"ev":"%s","room":"%s","name":"%s","email":"%s","id":"%s"}\n',
				os.time(), ev, jesc(room.jid:match("^([^@]+)@")), jesc(w.name), jesc(w.email), jesc(key)));
			f:close();
		end
	end);
	if not okp then module:log("warn", "mmn presence failed: %s", tostring(errp)); end
end
module:hook("muc-occupant-joined", function(e) plog("join", e.room, e.occupant, e.origin, e.stanza); end, -3);
module:hook("muc-occupant-left", function(e) plog("leave", e.room, e.occupant, nil, nil); end, 10);
