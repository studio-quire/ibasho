// proccap: audio capture for Ibasho's hearing, as raw PCM on stdout (signed 16-bit little-endian, interleaved).
//
//   proccap <pid> [rate=16000] [channels=1]
//       What one process tree plays (Windows process loopback, Windows 10 2004 or later): the viewer and its
//       helpers (media, voice), nothing else playing on the machine. Ends when that process exits.
//   proccap --device <endpoint id> [rate=16000] [channels=1]
//       Everything played on one output device (device loopback). Refuses (exit 3) a device that's the
//       default output for any role (console, multimedia, communications), and stops (exit 3) if it becomes
//       one, since that would be recording whatever the machine plays.
//   proccap --list
//       Active output devices, one per line: <endpoint id> TAB <default roles, or -> TAB <name>.
//
// Silence comes out as zeros in real time, also when Windows sends no packets at all (an idle device), so a
// reader can tell where an utterance ends. Exit codes: 0 done (target gone or reader gone), 1 failure, 2 usage,
// 3 refused.
//
// Part of Ibasho (MIT). Written by Claude Opus 5.5.
#include <windows.h>
#include <mmdeviceapi.h>
#include <audioclient.h>
#include <audioclientactivationparams.h>
#include <functiondiscoverykeys_devpkey.h>
#include <wrl/client.h>
#include <wrl/implements.h>
#include <cstdio>
#include <cstdlib>
#include <cwchar>
#include <string>
#include <fcntl.h>
#include <io.h>

#pragma comment(lib, "mmdevapi.lib")
#pragma comment(lib, "ole32.lib")

using Microsoft::WRL::ComPtr;
using namespace Microsoft::WRL;

static int fail(const char* what, HRESULT hr)
{
    fprintf(stderr, "proccap: %s failed: 0x%08lx\n", what, (unsigned long)hr);
    return 1;
}

static std::string utf8(const wchar_t* w)
{
    if (!w) return {};
    int n = WideCharToMultiByte(CP_UTF8, 0, w, -1, nullptr, 0, nullptr, nullptr);
    std::string s(n > 0 ? n - 1 : 0, '\0');
    if (n > 1) WideCharToMultiByte(CP_UTF8, 0, w, -1, s.data(), n, nullptr, nullptr);
    return s;
}

// ---------- devices ----------

static ComPtr<IMMDeviceEnumerator> enumerator()
{
    ComPtr<IMMDeviceEnumerator> e;
    CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL, IID_PPV_ARGS(&e));
    return e;
}

static std::wstring device_id(IMMDevice* d)
{
    LPWSTR id = nullptr;
    std::wstring out;
    if (d && SUCCEEDED(d->GetId(&id)))
    {
        out = id;
        CoTaskMemFree(id);
    }
    return out;
}

// The roles this device is the default output for, as "console,multimedia,communications" (empty: none).
static std::string default_roles(IMMDeviceEnumerator* e, const std::wstring& id)
{
    static const struct { ERole role; const char* name; } roles[] = {
        { eConsole, "console" }, { eMultimedia, "multimedia" }, { eCommunications, "communications" } };
    std::string out;
    for (auto& r : roles)
    {
        ComPtr<IMMDevice> d;
        if (SUCCEEDED(e->GetDefaultAudioEndpoint(eRender, r.role, &d)) && _wcsicmp(device_id(d.Get()).c_str(), id.c_str()) == 0)
        {
            if (!out.empty()) out += ",";
            out += r.name;
        }
    }
    return out;
}

static int list_devices()
{
    auto e = enumerator();
    if (!e) return fail("MMDeviceEnumerator", E_FAIL);
    ComPtr<IMMDeviceCollection> all;
    HRESULT hr = e->EnumAudioEndpoints(eRender, DEVICE_STATE_ACTIVE, &all);
    if (FAILED(hr)) return fail("EnumAudioEndpoints", hr);
    UINT n = 0;
    all->GetCount(&n);
    for (UINT i = 0; i < n; i++)
    {
        ComPtr<IMMDevice> d;
        if (FAILED(all->Item(i, &d))) continue;
        std::wstring id = device_id(d.Get());
        std::string name;
        ComPtr<IPropertyStore> props;
        if (SUCCEEDED(d->OpenPropertyStore(STGM_READ, &props)))
        {
            PROPVARIANT v;
            PropVariantInit(&v);
            if (SUCCEEDED(props->GetValue(PKEY_Device_FriendlyName, &v)) && v.vt == VT_LPWSTR)
                name = utf8(v.pwszVal);
            PropVariantClear(&v);
        }
        for (char& c : name)
            if (c == '\t' || c == '\n' || c == '\r') c = ' ';
        std::string roles = default_roles(e.Get(), id);
        printf("%s\t%s\t%s\n", utf8(id.c_str()).c_str(), roles.empty() ? "-" : roles.c_str(), name.c_str());
    }
    return 0;
}

// ---------- process loopback activation ----------

class Activation : public RuntimeClass<RuntimeClassFlags<ClassicCom>, FtmBase, IActivateAudioInterfaceCompletionHandler>
{
public:
    HANDLE done = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    HRESULT result = E_FAIL;
    ComPtr<IAudioClient> client;

    STDMETHOD(ActivateCompleted)(IActivateAudioInterfaceAsyncOperation* op) override
    {
        ComPtr<IUnknown> unk;
        HRESULT hr = S_OK;
        op->GetActivateResult(&hr, &unk);
        result = hr;
        if (SUCCEEDED(hr))
            result = unk.As(&client);
        SetEvent(done);
        return S_OK;
    }
};

static HRESULT process_client(DWORD pid, ComPtr<IAudioClient>& client)
{
    AUDIOCLIENT_ACTIVATION_PARAMS params = {};
    params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK;
    params.ProcessLoopbackParams.TargetProcessId = pid;
    params.ProcessLoopbackParams.ProcessLoopbackMode = PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE;
    PROPVARIANT pv = {};
    pv.vt = VT_BLOB;
    pv.blob.cbSize = sizeof(params);
    pv.blob.pBlobData = (BYTE*)&params;
    auto act = Make<Activation>();
    ComPtr<IActivateAudioInterfaceAsyncOperation> op;
    HRESULT hr = ActivateAudioInterfaceAsync(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, __uuidof(IAudioClient), &pv,
                                             act.Get(), &op);
    if (FAILED(hr)) return hr;
    WaitForSingleObject(act->done, INFINITE);
    if (FAILED(act->result)) return act->result;
    client = act->client;
    return S_OK;
}

// ---------- capture ----------

static bool write_all(const BYTE* src, size_t bytes)
{
    static BYTE zeros[65536];
    while (bytes)
    {
        size_t n = bytes < sizeof(zeros) ? bytes : sizeof(zeros);
        if (fwrite(src ? src : zeros, 1, n, stdout) != n) return false;  // reader went away
        if (src) src += n;
        bytes -= n;
    }
    return true;
}

int wmain(int argc, wchar_t** argv)
{
    bool list = argc >= 2 && wcscmp(argv[1], L"--list") == 0;
    bool device = argc >= 3 && wcscmp(argv[1], L"--device") == 0;
    int first = device ? 3 : 2;
    if (!list && (argc < 2 || (!device && argv[1][0] == L'-')))
    {
        fprintf(stderr, "usage: proccap <pid> [rate=16000] [channels=1]\n"
                        "       proccap --device <endpoint id> [rate=16000] [channels=1]\n"
                        "       proccap --list\n");
        return 2;
    }
    HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (FAILED(hr)) return fail("CoInitializeEx", hr);
    if (list)
    {
        _setmode(_fileno(stdout), _O_BINARY);  // UTF-8 bytes as they are
        return list_devices();
    }
    DWORD rate = argc > first ? wcstoul(argv[first], nullptr, 10) : 16000;
    WORD channels = argc > first + 1 ? (WORD)_wtoi(argv[first + 1]) : 1;
    if (rate < 8000 || rate > 192000 || channels < 1 || channels > 8)
    {
        fprintf(stderr, "proccap: rate 8000-192000, channels 1-8\n");
        return 2;
    }
    _setmode(_fileno(stdout), _O_BINARY);

    ComPtr<IAudioClient> client;
    ComPtr<IMMDeviceEnumerator> enumr;
    std::wstring dev_id;
    HANDLE target = nullptr;
    if (device)
    {
        dev_id = argv[2];
        enumr = enumerator();
        if (!enumr) return fail("MMDeviceEnumerator", E_FAIL);
        std::string roles = default_roles(enumr.Get(), dev_id);
        if (!roles.empty())
        {
            fprintf(stderr, "proccap: refusing: that device is the default output (%s); it would record everything "
                            "the machine plays\n", roles.c_str());
            return 3;
        }
        ComPtr<IMMDevice> d;
        hr = enumr->GetDevice(dev_id.c_str(), &d);
        if (FAILED(hr)) return fail("GetDevice (see proccap --list)", hr);
        hr = d->Activate(__uuidof(IAudioClient), CLSCTX_ALL, nullptr, (void**)client.GetAddressOf());
        if (FAILED(hr)) return fail("IMMDevice::Activate", hr);
    }
    else
    {
        DWORD pid = wcstoul(argv[1], nullptr, 10);
        target = OpenProcess(SYNCHRONIZE, FALSE, pid);
        if (!target)
        {
            fprintf(stderr, "proccap: no process %lu\n", (unsigned long)pid);
            return 1;
        }
        hr = process_client(pid, client);
        if (FAILED(hr)) return fail("process loopback activation (needs Windows 10 2004 or later)", hr);
    }

    WAVEFORMATEX fmt = {};
    fmt.wFormatTag = WAVE_FORMAT_PCM;
    fmt.nChannels = channels;
    fmt.nSamplesPerSec = rate;
    fmt.wBitsPerSample = 16;
    fmt.nBlockAlign = channels * 2;
    fmt.nAvgBytesPerSec = rate * fmt.nBlockAlign;
    hr = client->Initialize(AUDCLNT_SHAREMODE_SHARED,
        AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK |
        AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY,
        200 * 10000, 0, &fmt, nullptr);
    if (FAILED(hr)) return fail("IAudioClient::Initialize", hr);
    HANDLE ready = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    client->SetEventHandle(ready);
    ComPtr<IAudioCaptureClient> cap;
    hr = client->GetService(IID_PPV_ARGS(&cap));
    if (FAILED(hr)) return fail("GetService(IAudioCaptureClient)", hr);
    hr = client->Start();
    if (FAILED(hr)) return fail("Start", hr);
    if (device)
        fprintf(stderr, "proccap: capturing device %s at %lu Hz x%u\n", utf8(dev_id.c_str()).c_str(),
                (unsigned long)rate, channels);
    else
        fprintf(stderr, "proccap: capturing pid %s at %lu Hz x%u\n", utf8(argv[1]).c_str(), (unsigned long)rate,
                channels);

    LARGE_INTEGER freq, t0, now;
    QueryPerformanceFrequency(&freq);
    QueryPerformanceCounter(&t0);
    unsigned long long written = 0;  // frames out so far
    ULONGLONG last_check = GetTickCount64();
    int code = 0;
    for (;;)
    {
        HANDLE waits[2] = { ready, target };
        DWORD w = WaitForMultipleObjects(target ? 2 : 1, waits, FALSE, 100);
        if (w == WAIT_OBJECT_0 + 1) break;  // the target process exited
        UINT32 frames = 0;
        bool ok = true;
        while (ok && SUCCEEDED(cap->GetNextPacketSize(&frames)) && frames)
        {
            BYTE* data = nullptr;
            DWORD flags = 0;
            if (FAILED(cap->GetBuffer(&data, &frames, &flags, nullptr, nullptr))) break;
            ok = write_all((flags & AUDCLNT_BUFFERFLAGS_SILENT) ? nullptr : data, (size_t)frames * fmt.nBlockAlign);
            written += frames;
            cap->ReleaseBuffer(frames);
        }
        if (!ok) break;
        // An idle endpoint sends nothing at all: fill the gap with silence, keeping 50 ms of slack for late packets.
        QueryPerformanceCounter(&now);
        unsigned long long due = (unsigned long long)((now.QuadPart - t0.QuadPart) * (double)rate / freq.QuadPart);
        if (due > written + rate / 10)
        {
            unsigned long long pad = due - written - rate / 20;
            if (!write_all(nullptr, (size_t)pad * fmt.nBlockAlign)) break;
            written += pad;
        }
        if (fflush(stdout) != 0 || ferror(stdout)) break;  // reader went away (fwrite alone only fills the buffer)
        if (device && GetTickCount64() - last_check > 2000)
        {
            last_check = GetTickCount64();
            std::string roles = default_roles(enumr.Get(), dev_id);
            if (!roles.empty())
            {
                fprintf(stderr, "proccap: stopping: the device became the default output (%s)\n", roles.c_str());
                code = 3;
                break;
            }
        }
    }
    client->Stop();
    return code;
}
