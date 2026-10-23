// Throwaway (ibasho contents): reports an object's real contents straight from the region, after removing the
// items named in GONE, then deletes itself. The script editor's listing can be stale; this can't.
default
{
    state_entry()
    {
        string me = llGetScriptName();
        list gone = [$gone];
        integer i;
        for (i = 0; i < llGetListLength(gone); ++i)
        {
            string g = llList2String(gone, i);
            if (g != me && llGetInventoryType(g) != INVENTORY_NONE) llRemoveInventory(g);
        }
        integer n = llGetInventoryNumber(INVENTORY_ALL);
        for (i = 0; i < n; ++i)
        {
            string m = llGetInventoryName(INVENTORY_ALL, i);
            if (m != me)
            {
                integer t = llGetInventoryType(m);
                string run = "";
                if (t == INVENTORY_SCRIPT) run = (string)llGetScriptState(m);
                llOwnerSay("IBASHO|inv|$tag|" + m + "|" + (string)t + "|" + run);
            }
        }
        llOwnerSay("IBASHO|inv|$tag|EOF");
        llRemoveInventory(me);
    }
}
