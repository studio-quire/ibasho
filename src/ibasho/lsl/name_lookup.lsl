// Ibasho name lookup: the avatar id for a username, asked of the grid through the region (llRequestUserKey).
// `ibasho im`, `pay`, `give`, `friend` and `profile` drop it into a temporary prim when the viewer has no avatar
// search of its own. NAMES are ways of writing the one name (some grids match case exactly), asked in turn. It
// owner-says "IBASHO|user|<tag>|<id>" for the first found (the null key if none was, TIMEOUT if the region never
// answered), then removes itself.
list NAMES = [$names];
string TAG = "$tag";
integer next;
key query;

done(string answer)
{
    llOwnerSay("IBASHO|user|" + TAG + "|" + answer);
    llRemoveInventory(llGetScriptName());
}

ask()
{
    query = llRequestUserKey(llList2String(NAMES, next));
    ++next;
    llSetTimerEvent(20.0);
}

default
{
    state_entry()
    {
        ask();
    }

    dataserver(key id, string data)
    {
        if (id != query) return;
        if ((key)data != NULL_KEY || next >= llGetListLength(NAMES)) done(data);
        else ask();
    }

    timer()
    {
        done("TIMEOUT");
    }
}
