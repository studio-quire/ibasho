// Throwaway (ibasho notecard): reads a notecard by its asset id and owner-says it line by line, then deletes
// itself. Cards holding embedded items (landmarks and such) read as empty this way.
key CARD = "$card";
string TAG = "IBASHO|nc|$tag|";
integer line;
key req;

default
{
    state_entry() { req = llGetNotecardLine(CARD, line); llSetTimerEvent(60.0); }

    dataserver(key q, string d)
    {
        if (q != req) return;
        if (d == EOF) { llOwnerSay(TAG + "EOF"); llRemoveInventory(llGetScriptName()); return; }
        if (d == NAK) { req = llGetNotecardLine(CARD, line); return; }
        llOwnerSay(TAG + (string)line + "|" + d);
        ++line;
        req = llGetNotecardLine(CARD, line);
    }

    timer() { llOwnerSay(TAG + "TIMEOUT"); llRemoveInventory(llGetScriptName()); }
}
