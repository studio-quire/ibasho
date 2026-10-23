// Ibasho self-check: is the owner's account flagged as a scripted agent, and does its display name say AI?
// `ibasho selfcheck` drops this into a prim and reads the IBASHO| line. You can also put it in any prim you own
// by hand: it tells you in plain words, then removes itself.
default
{
    state_entry()
    {
        key me = llGetOwner();
        integer info = llGetAgentInfo(me);
        integer flagged = (info & 0x4000) != 0;    // AGENT_AUTOMATED (the literal compiles on grids without it)
        string shown = llGetDisplayName(me);
        string verdict = "NOT flagged as a scripted agent: flag it at https://secondlife.com/my/account/sisa.php";
        if (flagged) verdict = "flagged as a scripted agent";
        llOwnerSay("self-check: " + verdict + "; display name \"" + shown + "\"");
        llOwnerSay("IBASHO|selfcheck|" + (string)flagged + "|" + (string)info + "|" + shown);
        llRemoveInventory(llGetScriptName());
    }
}
