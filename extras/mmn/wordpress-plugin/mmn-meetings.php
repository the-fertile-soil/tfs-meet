<?php
/**
 * Plugin Name: Example Org Meetings
 * Description: Lets WordPress administrators schedule Example Org video sessions from a page. Add the shortcode [mmn_meetings] to any page.
 * Version: 1.0
 */
if (!defined('ABSPATH')) { exit; }

function mmn_meet_cfg() {
    static $cfg = null;
    if ($cfg === null) {
        $f = __DIR__ . '/config.php';
        $cfg = file_exists($f) ? include $f : array();
    }
    return $cfg;
}

function mmn_meet_call($method, $path, $body = null) {
    $cfg = mmn_meet_cfg();
    if (empty($cfg['key']) || empty($cfg['base'])) {
        return new WP_Error('mmn', 'The meeting service is not configured.');
    }
    $args = array(
        'method' => $method,
        'timeout' => 90,
        'headers' => array('X-MMN-Key' => $cfg['key'], 'X-Requested-With' => 'tfs', 'Content-Type' => 'application/json'),
    );
    if ($body !== null) { $args['body'] = wp_json_encode($body); }
    $r = wp_remote_request(rtrim($cfg['base'], '/') . $path, $args);
    if (is_wp_error($r)) { return $r; }
    $code = wp_remote_retrieve_response_code($r);
    $data = json_decode(wp_remote_retrieve_body($r), true);
    if ($code >= 300) {
        $msg = (is_array($data) && !empty($data['detail'])) ? $data['detail'] : 'The meeting service said no (' . $code . ').';
        if (is_array($msg)) { $msg = 'Please check the form and try again.'; }
        return new WP_Error('mmn', $msg);
    }
    return is_array($data) ? $data : array();
}

function mmn_meet_guard() {
    if (!is_user_logged_in() || !current_user_can('manage_options')) {
        wp_send_json_error(array('message' => 'Only site administrators can schedule sessions.'), 403);
    }
    check_ajax_referer('mmn_meet', 'nonce');
}

add_action('wp_ajax_mmn_schedule', function () {
    mmn_meet_guard();
    $u = wp_get_current_user();
    $payload = array(
        'title' => sanitize_text_field(wp_unslash($_POST['title'] ?? '')),
        'when' => sanitize_text_field(wp_unslash($_POST['when'] ?? '')),
        'tz' => sanitize_text_field(wp_unslash($_POST['tz'] ?? '')),
        'host' => $u->user_email,
        'emails' => sanitize_textarea_field(wp_unslash($_POST['emails'] ?? '')),
        'host_attends' => !empty($_POST['host_attends']),
        'now' => !empty($_POST['now']),
    );
    $r = mmn_meet_call('POST', '/api/wp/schedule', $payload);
    if (is_wp_error($r)) { wp_send_json_error(array('message' => $r->get_error_message())); }
    wp_send_json_success($r);
});

add_action('wp_ajax_mmn_list', function () {
    mmn_meet_guard();
    $r = mmn_meet_call('GET', '/api/wp/list');
    if (is_wp_error($r)) { wp_send_json_error(array('message' => $r->get_error_message())); }
    wp_send_json_success($r);
});

add_action('wp_ajax_mmn_cancel', function () {
    mmn_meet_guard();
    $room = preg_replace('/[^a-z0-9-]/', '', strtolower(wp_unslash($_POST['room'] ?? '')));
    $r = mmn_meet_call('POST', '/api/wp/cancel/' . $room, array());
    if (is_wp_error($r)) { wp_send_json_error(array('message' => $r->get_error_message())); }
    wp_send_json_success($r);
});

add_shortcode('mmn_meetings', function () {
    if (!is_user_logged_in()) {
        return '<div class="mmn-m"><p>Sessions are scheduled by Example Org administrators.</p><p><a class="mmn-btn" href="' . esc_url(wp_login_url(get_permalink())) . '">Administrator log in</a></p></div>' . mmn_meet_css();
    }
    if (!current_user_can('manage_options')) {
        return '<div class="mmn-m"><p>Only Example Org administrators can schedule sessions. If you were invited to a session, please use the link in your invitation email.</p></div>' . mmn_meet_css();
    }
    $nonce = wp_create_nonce('mmn_meet');
    $ajax = admin_url('admin-ajax.php');
    $zones = array(
        'America/Los_Angeles' => 'Pacific', 'America/Denver' => 'Mountain', 'America/Chicago' => 'Central',
        'America/New_York' => 'Eastern', 'America/Anchorage' => 'Alaska', 'Pacific/Honolulu' => 'Hawaii',
    );
    $opts = '';
    foreach ($zones as $id => $label) { $opts .= '<option value="' . esc_attr($id) . '">' . esc_html($label) . '</option>'; }
    $times = '';
    for ($h = 6; $h <= 21; $h++) {
        foreach (array(0, 15, 30, 45) as $m) {
            $v = sprintf('%02d:%02d', $h, $m);
            $hh = $h % 12 ? $h % 12 : 12;
            $lab = $hh . ':' . sprintf('%02d', $m) . ($h < 12 ? ' AM' : ' PM');
            $times .= '<option value="' . $v . '"' . ($v === '15:00' ? ' selected' : '') . '>' . $lab . '</option>';
        }
    }
    ob_start(); ?>
<div class="mmn-m" id="mmn-m">
  <h2>Schedule a session</h2>
  <p class="mmn-sub">Sessions are 30 minutes with up to 4 people. Everyone you invite gets an email and can start the session from their invitation, no host needed.</p>
  <form id="mmn-f">
    <label>When</label>
    <label class="mmn-chk"><input type="radio" name="mmn-w" value="now"> Now (starts right away, skips the calendar)</label>
    <label class="mmn-chk"><input type="radio" name="mmn-w" value="later" checked> Later (pick a date and time)</label>
    <label for="mmn-t">Session name</label>
    <input id="mmn-t" type="text" maxlength="120" placeholder="Career chat with a software engineer" required>
    <div class="mmn-two" id="mmn-when">
      <div><label for="mmn-d">Date</label><input id="mmn-d" type="date"></div>
      <div><label for="mmn-tm">Time</label><select id="mmn-tm"><?php echo $times; ?></select></div>
    </div>
    <div id="mmn-zrow"><label for="mmn-z">Time zone</label>
    <select id="mmn-z"><?php echo $opts; ?></select></div>
    <label for="mmn-e">Emails of the people to invite (up to 4 in total, separated by commas or new lines)</label>
    <textarea id="mmn-e" rows="3" maxlength="600" placeholder="student@example.org, mentor@example.org"></textarea>
    <label class="mmn-chk"><input id="mmn-h" type="checkbox" checked> I am also joining this session (counts as 1 of the 4)</label>
    <p><button class="mmn-btn" type="submit" id="mmn-b">Schedule and send invitations</button></p>
    <p id="mmn-msg"></p>
  </form>
  <h2>Upcoming sessions</h2>
  <div id="mmn-list"><p class="mmn-sub">Loading...</p></div>
</div>
<?php echo mmn_meet_css(); ?>
<script>
(function(){
var AJAX=<?php echo wp_json_encode($ajax); ?>,NONCE=<?php echo wp_json_encode($nonce); ?>;
function post(action,data){var fd=new FormData();fd.append("action",action);fd.append("nonce",NONCE);for(var k in data){fd.append(k,data[k]);}
 return fetch(AJAX,{method:"POST",credentials:"same-origin",body:fd}).then(function(r){return r.json();});}
function esc(s){return String(s).replace(/[&<>"]/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c];});}
var d=document.getElementById("mmn-d");var t=new Date();t.setMinutes(t.getMinutes()-t.getTimezoneOffset());d.min=t.toISOString().slice(0,10);
function load(){post("mmn_list",{}).then(function(r){var L=document.getElementById("mmn-list");
 if(!r.success){L.innerHTML='<p class="mmn-err">'+esc((r.data&&r.data.message)||"Could not load sessions.")+'</p>';return;}
 var ms=r.data.meetings||[];if(!ms.length){L.innerHTML='<p class="mmn-sub">No sessions are scheduled.</p>';return;}
 var h='<table class="mmn-tbl"><thead><tr><th>Session</th><th>When</th><th>Invited</th><th>Link</th><th></th></tr></thead><tbody>';
 ms.forEach(function(m){h+='<tr><td>'+esc(m.title)+'</td><td>'+esc(m.when)+'</td><td>'+esc((m.attendees||[]).join(", "))+'</td><td><a href="'+esc(m.url)+'">Link</a></td><td><button class="mmn-btn mmn-o" data-room="'+esc(m.room)+'">Cancel</button></td></tr>';});
 L.innerHTML=h+'</tbody></table>';
 L.querySelectorAll("button[data-room]").forEach(function(b){b.onclick=function(){if(!confirm("Cancel this session and tell everyone invited?"))return;b.disabled=true;
  post("mmn_cancel",{room:b.getAttribute("data-room")}).then(function(){load();});};});});}
function isNow(){return document.querySelector("input[name=mmn-w]:checked").value==="now";}
function toggle(){var n=isNow();document.getElementById("mmn-when").style.display=n?"none":"";document.getElementById("mmn-zrow").style.display=n?"none":"";document.getElementById("mmn-d").required=!n;}
document.querySelectorAll("input[name=mmn-w]").forEach(function(r){r.onchange=toggle;});toggle();
document.getElementById("mmn-f").onsubmit=function(e){e.preventDefault();var m=document.getElementById("mmn-msg"),b=document.getElementById("mmn-b");
 m.className="";m.textContent="Working...";b.disabled=true;
 var nowFlag=isNow();
 post("mmn_schedule",{now:nowFlag?"1":"",title:document.getElementById("mmn-t").value,when:d.value+"T"+document.getElementById("mmn-tm").value,tz:document.getElementById("mmn-z").value,
  emails:document.getElementById("mmn-e").value,host_attends:document.getElementById("mmn-h").checked?"1":""})
 .then(function(r){b.disabled=false;
  if(r.success){m.className="mmn-ok";m.innerHTML=(nowFlag?"Your session is ready. ":"Scheduled. ")+(r.data.invited||0)+" invitation(s) sent. <a href=\""+esc(r.data.url)+"\" target=\"_blank\" rel=\"noopener\">"+(nowFlag?"Open the session":"Link")+"</a>";document.getElementById("mmn-f").reset();toggle();load();if(nowFlag){window.open(r.data.url,"_blank");}}
  else{m.className="mmn-err";m.textContent=(r.data&&r.data.message)||"Something went wrong.";}})
 .catch(function(){b.disabled=false;m.className="mmn-err";m.textContent="Could not reach the server. Please try again.";});};
load();
})();
</script>
<?php
    return ob_get_clean();
});

function mmn_meet_css() {
    return '<style>
.mmn-m{max-width:760px;margin:0 auto;font-family:Nunito,system-ui,sans-serif;color:#241a4a}
.mmn-m h2{font-family:Fredoka,Nunito,sans-serif;color:#241a4a;margin:1.2em 0 .3em}
.mmn-sub{color:#5b5280}
.mmn-m label{display:block;font-weight:700;margin:14px 0 5px}
.mmn-m input[type=text],.mmn-m input[type=date],.mmn-m select,.mmn-m textarea{width:100%;padding:12px 14px;border:2px solid #bdb3dd;border-radius:12px;font-size:1rem;font-family:inherit;background:#fff;box-sizing:border-box}
.mmn-two{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:600px){.mmn-two{grid-template-columns:1fr}}
.mmn-chk{font-weight:600!important}
.mmn-btn{display:inline-block;padding:12px 24px;border-radius:999px;background:#6a3fd0;color:#fff!important;font-weight:800;border:2px solid #6a3fd0;cursor:pointer;font-size:1rem;text-decoration:none;font-family:Fredoka,Nunito,sans-serif}
.mmn-btn:hover{background:#4f2db0;border-color:#4f2db0}
.mmn-btn.mmn-o{background:transparent;color:#4f2db0!important;padding:6px 14px;font-size:.9rem}
.mmn-ok{color:#2f5a1f;font-weight:700}.mmn-err{color:#b0245a;font-weight:700}
.mmn-tbl{width:100%;border-collapse:collapse;background:#fff}
.mmn-tbl th,.mmn-tbl td{text-align:left;vertical-align:top;padding:10px;border-bottom:1px solid #e3dcf5;font-size:.95rem}
.mmn-tbl th{background:#f1ecff}
</style>';
}
