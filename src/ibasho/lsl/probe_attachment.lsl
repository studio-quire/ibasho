// Ibasho ground probe, for viewers without IbashoAgent probeGround. Wear it (any attachment point; it makes
// itself tiny and invisible) and set probe.attachment_channel in the config to CHANNEL below.
//
// The owner says on CHANNEL: "probe [BEARING] [DIST] [STEP]" (bearing in compass degrees, 0 = north, 90 = east;
// "-" or none = the way the avatar faces). It casts rays down every STEP m out to DIST m and owner-says
// "IBASHO|probe|<feet z>|d1:dz1,d2:dz2,...", dz being the ground's height relative to the feet
// ("X" = no ground within ~40 m below, "?" = the region refused the ray).
integer CHANNEL = 7735;

float ground(vector p, float top)
{
    list r;
    integer n = -1;
    integer tries;
    for (tries = 0; tries < 4 && n < 0; ++tries)        // n < 0: the region refused the cast (busy), not "no ground"
    {
        if (tries) llSleep(0.1 * tries);
        r = llCastRay(<p.x, p.y, top>, <p.x, p.y, top - 42>,
            [RC_REJECT_TYPES, RC_REJECT_AGENTS, RC_DATA_FLAGS, RC_GET_ROOT_KEY, RC_MAX_HITS, 3]);
        n = llList2Integer(r, -1);
    }
    if (n < 0) return -8888.0;
    integer i;
    for (i = 0; i < n; ++i)
    {
        if (llList2Key(r, i * 2) != llGetKey())
        {
            vector hit = llList2Vector(r, i * 2 + 1);    // LSL can't take .z of a call's result directly
            return hit.z;
        }
    }
    // No hit at all, not even terrain: it happens on some regions with solid ground underfoot. Use the terrain
    // height, and count open water as a drop so rivers still warn.
    vector off = p - llGetPos();
    float t = llGround(<off.x, off.y, 0>);
    float w = llWater(<off.x, off.y, 0>);
    if (w > t) t = w - 1.5;
    if (t > top - 42) return t;
    return -9999.0;
}

default
{
    state_entry()
    {
        llListen(CHANNEL, "", llGetOwner(), "");
        llSetPrimitiveParams([PRIM_SIZE, <0.05, 0.05, 0.05>, PRIM_COLOR, ALL_SIDES, <1, 1, 1>, 0.0]);
    }

    on_rez(integer n) { llResetScript(); }

    listen(integer ch, string name, key id, string msg)
    {
        list w = llParseString2List(msg, [" "], []);
        if (llList2String(w, 0) != "probe") return;
        vector me = llGetPos();                                    // an attachment's position is the avatar's
        vector size = llGetAgentSize(llGetOwner());
        float feet = me.z - size.z / 2;
        float yaw;
        if (llGetListLength(w) > 1 && llList2String(w, 1) != "-")
            yaw = (90.0 - llList2Float(w, 1)) * DEG_TO_RAD;      // compass bearing -> math angle
        else
        {
            vector f = llRot2Fwd(llGetRootRotation());
            yaw = llAtan2(f.y, f.x);
        }
        float dist = 8;
        float step = 1;
        if (llGetListLength(w) > 2) dist = llList2Float(w, 2);
        if (llGetListLength(w) > 3) step = llList2Float(w, 3);
        vector dir = <llCos(yaw), llSin(yaw), 0>;
        string out = "";
        float d;
        for (d = step; d <= dist + 0.001; d += step)
        {
            float z = ground(me + dir * d, feet + 3);
            string v = "X";
            if (z == -8888.0) v = "?";
            else if (z > -9000) v = llGetSubString((string)(z - feet), 0, -5);
            if (out != "") out += ",";
            out += llGetSubString((string)d, 0, -5) + ":" + v;
        }
        llOwnerSay("IBASHO|probe|" + llGetSubString((string)feet, 0, -5) + "|" + out);
    }
}
