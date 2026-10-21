-- TFS: close meeting rooms when their time is up.
-- Walk-in meetings start at 30 minutes and the host can add 30 minutes at a time up to 90.
-- The web app writes per-room limits to /config/tfs-limits.json: {"room": {"minutes": 15}}.
-- Rooms with no entry (scheduled meetings) get the full 90 minutes.
-- The browser shows the countdown and the "add 15 minutes" prompt; this module only enforces the end.
local json = require "util.json";
local is_healthcheck_room = module:require "util".is_healthcheck_room;

local DEFAULT_MIN = module:get_option_number("tfs_meeting_limit_seconds", 5400) / 60;
local LIMITS = "/config/tfs-limits.json";

local muc_service = prosody.hosts[module.host].modules.muc;

local function read_limits()
	local f = io.open(LIMITS, "r");
	if not f then return {}; end
	local txt = f:read("*a");
	f:close();
	local ok, d = pcall(json.decode, txt);
	if ok and type(d) == "table" then return d; end
	return {};
end

local function on_room_created(event)
	local room = event.room;
	if is_healthcheck_room(room.jid) then return; end
	room._tfs_started = os.time();
	module:log("info", "TFS time limit: tracking room %s", room.jid);
end

local function sweep()
	local ok, err = pcall(function()
		local limits = read_limits();
		local now = os.time();
		local rooms = muc_service.live_rooms and muc_service.live_rooms() or muc_service.each_room();
		local to_close = {};
		for room in rooms do
			if not is_healthcheck_room(room.jid) then
				local name = room.jid:match("^([^@]+)@");
				room._tfs_started = room._tfs_started or now;
				local e = limits[name];
				local minutes = (e and tonumber(e.minutes)) or DEFAULT_MIN;
				if now - room._tfs_started >= minutes * 60 then
					to_close[#to_close + 1] = room;
				end
			end
		end
		for _, room in ipairs(to_close) do
			module:log("info", "TFS time limit: closing room %s", room.jid);
			room:destroy(nil, "This meeting reached its time limit.");
		end
	end);
	if not ok then module:log("warn", "tfs_time_limit sweep failed: %s", tostring(err)); end
end

module:hook("muc-room-created", on_room_created, -1);
module:add_timer(10, function() sweep(); return 10; end);
