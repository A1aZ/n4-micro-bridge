/*
 * Mirabox N4 -> Codex Micro bridge sideband contract.
 *
 * This header is intentionally independent of the HID minidriver.  The
 * custom-GUID/IOCTL interface below is retained as a legacy contract scaffold
 * for review and offline probes; Microsoft's vhidmini2 guidance means it must
 * not be treated as the production user-mode transport.  The active HID
 * descriptor contains the Codex collection (0xFF00/ID 6) plus the companion
 * collection (0xFF70/ID 7).  Both collections use one shared READ_REPORT
 * stream; the driver translates only the report ID at the bridge boundary.
 */
#ifndef MIRABOX_CODEX_MICRO_PROTOCOL_H
#define MIRABOX_CODEX_MICRO_PROTOCOL_H

#include <guiddef.h>
#ifndef CTL_CODE
/* WDF/HID headers may have already loaded winioctl.h. Its GUID definitions
 * intentionally sit outside the include guard; with INITGUID set, including
 * it again would instantiate the Windows device GUIDs a second time. */
#include <winioctl.h>
#endif

#define MIRABOX_CODEX_MICRO_VID             0x303A
#define MIRABOX_CODEX_MICRO_PID             0x8360
#define MIRABOX_CODEX_MICRO_RELEASE         0x0100
#define MIRABOX_CODEX_MICRO_USAGE_PAGE      0xFF00
#define MIRABOX_CODEX_MICRO_USAGE           0x0001
#define MIRABOX_CODEX_MICRO_REPORT_ID       0x06
#define MIRABOX_CODEX_MICRO_REPORT_LENGTH   64
#define MIRABOX_CODEX_MICRO_BODY_LENGTH     63
#define MIRABOX_CODEX_MICRO_MESSAGE_TYPE    0x02
#define MIRABOX_CODEX_MICRO_PAYLOAD_MAX     61

/*
 * Companion HID top-level collection (TLC).
 *
 * This collection deliberately uses a different vendor page and report ID
 * from the Codex collection.  The checked-in UMDF source advertises the dual
 * descriptor and uses a shared HID input stream (READ_REPORT has no requested
 * report ID), explicit report-ID translation, and safe routing for both TLCs.
 * A separate legacy sideband output queue may coexist for diagnostics, but a
 * per-TLC READ_REPORT queue is not a substitute for HIDClass's shared input
 * path.
 */
#define MIRABOX_CODEX_COMPANION_USAGE_PAGE       0xFF70
#define MIRABOX_CODEX_COMPANION_USAGE            0x0001
#define MIRABOX_CODEX_COMPANION_REPORT_ID        0x07
#define MIRABOX_CODEX_COMPANION_REPORT_LENGTH    64
#define MIRABOX_CODEX_COMPANION_BODY_LENGTH      63

/*
 * A separate device interface is used for the bridge process.  It is not a
 * HID collection and therefore does not alter Codex's usage-page discovery.
 */
DEFINE_GUID(GUID_DEVINTERFACE_MIRABOX_CODEX_MICRO_BRIDGE,
    0x7f5a2e91, 0x1c0e, 0x4d1e, 0x9c, 0x2a, 0x1a, 0x6b, 0x8e, 0x53, 0x70, 0x44);

#define IOCTL_MIRABOX_CODEX_PUSH_INPUT \
    CTL_CODE(FILE_DEVICE_UNKNOWN, 0x801, METHOD_BUFFERED, FILE_WRITE_ACCESS)
#define IOCTL_MIRABOX_CODEX_READ_OUTPUT \
    CTL_CODE(FILE_DEVICE_UNKNOWN, 0x802, METHOD_BUFFERED, FILE_READ_ACCESS)
#define IOCTL_MIRABOX_CODEX_GET_INFO \
    CTL_CODE(FILE_DEVICE_UNKNOWN, 0x803, METHOD_BUFFERED, FILE_READ_ACCESS)
#define IOCTL_MIRABOX_CODEX_RESET \
    CTL_CODE(FILE_DEVICE_UNKNOWN, 0x804, METHOD_BUFFERED, FILE_WRITE_ACCESS)

#pragma pack(push, 1)
typedef struct _MIRABOX_CODEX_REPORT {
    UCHAR Bytes[MIRABOX_CODEX_MICRO_REPORT_LENGTH];
} MIRABOX_CODEX_REPORT, *PMIRABOX_CODEX_REPORT;

typedef struct _MIRABOX_CODEX_INFO {
    USHORT VendorId;
    USHORT ProductId;
    USHORT Release;
    USHORT UsagePage;
    UCHAR ReportId;
    UCHAR ReportLength;
    UCHAR Reserved[2];
} MIRABOX_CODEX_INFO, *PMIRABOX_CODEX_INFO;
#pragma pack(pop)

/*
 * The report descriptor is kept here as a reviewable, transport-neutral
 * specification.  codexmicro.c contains the same bytes for the HID class
 * callback; a later refactor can make this array the single source of truth.
 */
static const UCHAR MIRABOX_CODEX_MICRO_REPORT_DESCRIPTOR[] = {
    0x06, 0x00, 0xFF,       /* Usage Page (Vendor Defined 0xFF00) */
    0x09, 0x01,             /* Usage (1) */
    0xA1, 0x01,             /* Collection (Application) */
    0x85, 0x06,             /*   Report ID (6) */
    0x15, 0x00,             /*   Logical Minimum (0) */
    0x26, 0xFF, 0x00,       /*   Logical Maximum (255) */
    0x75, 0x08,             /*   Report Size (8) */
    0x95, 0x3F,             /*   Report Count (63) */
    0x09, 0x01,             /*   Usage (1) */
    0x81, 0x02,             /*   Input (Data,Var,Abs) */
    0x95, 0x3F,             /*   Report Count (63) */
    0x09, 0x02,             /*   Usage (2) */
    0x91, 0x02,             /*   Output (Data,Var,Abs) */
    0xC0                    /* End Collection */
};

#define MIRABOX_CODEX_MICRO_REPORT_DESCRIPTOR_LENGTH \
    ((USHORT)sizeof(MIRABOX_CODEX_MICRO_REPORT_DESCRIPTOR))

/*
 * The companion descriptor mirrors the 64-byte framing of the Codex
 * collection, but has its own usage page (0xFF70) and report ID (7).  A
 * native bridge writes ID 7 here and the active driver translates the body to
 * an ID-6 input report for Codex.  Conversely, Codex ID-6 output is exposed
 * as an ID-7 input report to the bridge.  No JSON or extra opcode is visible
 * in this collection.
 */
static const UCHAR MIRABOX_CODEX_COMPANION_REPORT_DESCRIPTOR[] = {
    0x06, 0x70, 0xFF,       /* Usage Page (Vendor Defined 0xFF70) */
    0x09, 0x01,             /* Usage (1) */
    0xA1, 0x01,             /* Collection (Application) */
    0x85, 0x07,             /*   Report ID (7) */
    0x15, 0x00,             /*   Logical Minimum (0) */
    0x26, 0xFF, 0x00,       /*   Logical Maximum (255) */
    0x75, 0x08,             /*   Report Size (8) */
    0x95, 0x3F,             /*   Report Count (63) */
    0x09, 0x01,             /*   Usage (1) */
    0x81, 0x02,             /*   Input (Data,Var,Abs) */
    0x95, 0x3F,             /*   Report Count (63) */
    0x09, 0x02,             /*   Usage (2) */
    0x91, 0x02,             /*   Output (Data,Var,Abs) */
    0xC0                    /* End Collection */
};

#define MIRABOX_CODEX_COMPANION_REPORT_DESCRIPTOR_LENGTH \
    ((USHORT)sizeof(MIRABOX_CODEX_COMPANION_REPORT_DESCRIPTOR))

/*
 * Complete two-TLC descriptor used by the active driver.  Keeping this
 * byte-for-byte array in the protocol header makes the HID shape reviewable
 * and testable without a WDK; codexmicro.c carries the same bytes in its
 * callback-visible descriptor until the WDK build is available.
 */
static const UCHAR MIRABOX_CODEX_MICRO_DUAL_REPORT_DESCRIPTOR[] = {
    /* Primary Codex collection: 0xFF00 / report ID 6. */
    0x06, 0x00, 0xFF,
    0x09, 0x01,
    0xA1, 0x01,
    0x85, 0x06,
    0x15, 0x00,
    0x26, 0xFF, 0x00,
    0x75, 0x08,
    0x95, 0x3F,
    0x09, 0x01,
    0x81, 0x02,
    0x95, 0x3F,
    0x09, 0x02,
    0x91, 0x02,
    0xC0,
    /* Companion collection: 0xFF70 / report ID 7. */
    0x06, 0x70, 0xFF,
    0x09, 0x01,
    0xA1, 0x01,
    0x85, 0x07,
    0x15, 0x00,
    0x26, 0xFF, 0x00,
    0x75, 0x08,
    0x95, 0x3F,
    0x09, 0x01,
    0x81, 0x02,
    0x95, 0x3F,
    0x09, 0x02,
    0x91, 0x02,
    0xC0
};

#define MIRABOX_CODEX_MICRO_DUAL_REPORT_DESCRIPTOR_LENGTH \
    ((USHORT)sizeof(MIRABOX_CODEX_MICRO_DUAL_REPORT_DESCRIPTOR))

#endif /* MIRABOX_CODEX_MICRO_PROTOCOL_H */
