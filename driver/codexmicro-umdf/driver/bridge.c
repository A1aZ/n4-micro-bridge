/*
 * Sideband bridge plumbing for the Mirabox N4 companion process.
 *
 * The HID class stack owns the virtual Codex collection.  A separate device
 * interface lets a normal user-mode process push a complete 64-byte input
 * report and read the output reports that Codex sends back.  Keeping this
 * path in its own translation unit makes it easy to replace the sample's
 * timer with the production N4/hidapi reader later.
 */

#include "codexmicro.h"

static NTSTATUS
CompleteReportRequest(
    _In_ WDFREQUEST Request,
    _In_reads_(MIRABOX_CODEX_MICRO_REPORT_LENGTH) const UCHAR *Report
    )
{
    WDFMEMORY memory;
    size_t length;
    NTSTATUS status;

    status = WdfRequestRetrieveOutputMemory(Request, &memory);
    if (!NT_SUCCESS(status)) {
        return status;
    }

    WdfMemoryGetBuffer(memory, &length);
    if (length < MIRABOX_CODEX_MICRO_REPORT_LENGTH) {
        return STATUS_BUFFER_TOO_SMALL;
    }

    status = WdfMemoryCopyFromBuffer(
        memory, 0, (PVOID)Report, MIRABOX_CODEX_MICRO_REPORT_LENGTH);
    if (NT_SUCCESS(status)) {
        WdfRequestSetInformation(Request, MIRABOX_CODEX_MICRO_REPORT_LENGTH);
    }
    return status;
}

static NTSTATUS
CopyInputReport(
    _In_ WDFREQUEST Request,
    _Out_writes_(MIRABOX_CODEX_MICRO_REPORT_LENGTH) UCHAR *Report
    )
{
    WDFMEMORY memory;
    size_t length;
    NTSTATUS status;

    status = WdfRequestRetrieveInputMemory(Request, &memory);
    if (!NT_SUCCESS(status)) {
        return status;
    }

    WdfMemoryGetBuffer(memory, &length);
    if (length != MIRABOX_CODEX_MICRO_REPORT_LENGTH) {
        return STATUS_INVALID_BUFFER_SIZE;
    }

    return WdfMemoryCopyToBuffer(
        memory, 0, Report, MIRABOX_CODEX_MICRO_REPORT_LENGTH);
}

static BOOLEAN
BridgeTryPopInputLocked(
    _In_ PDEVICE_CONTEXT DeviceContext,
    _Out_writes_(MIRABOX_CODEX_MICRO_REPORT_LENGTH) UCHAR *Report
    )
{
    if (DeviceContext->InputCount == 0) {
        return FALSE;
    }

    RtlCopyMemory(
        Report,
        DeviceContext->InputRing[DeviceContext->InputHead],
        MIRABOX_CODEX_MICRO_REPORT_LENGTH);
    DeviceContext->InputHead =
        (DeviceContext->InputHead + 1) % MIRABOX_INPUT_RING_CAPACITY;
    DeviceContext->InputCount -= 1;
    return TRUE;
}

static VOID
BridgeStoreInputLocked(
    _In_ PDEVICE_CONTEXT DeviceContext,
    _In_reads_(MIRABOX_CODEX_MICRO_REPORT_LENGTH) const UCHAR *Report
    )
{
    ULONG slot;

    // Drop the oldest event on overflow. This keeps a fresh release/event
    // available instead of wedging the HID reader behind stale traffic.
    if (DeviceContext->InputCount == MIRABOX_INPUT_RING_CAPACITY) {
        DeviceContext->InputHead =
            (DeviceContext->InputHead + 1) % MIRABOX_INPUT_RING_CAPACITY;
        DeviceContext->InputCount -= 1;
    }
    slot = (DeviceContext->InputHead + DeviceContext->InputCount) %
           MIRABOX_INPUT_RING_CAPACITY;
    RtlCopyMemory(
        DeviceContext->InputRing[slot],
        Report,
        MIRABOX_CODEX_MICRO_REPORT_LENGTH);
    DeviceContext->InputCount += 1;
}

BOOLEAN
BridgeTryPopInput(
    _In_ PDEVICE_CONTEXT DeviceContext,
    _Out_writes_(MIRABOX_CODEX_MICRO_REPORT_LENGTH) UCHAR *Report
    )
{
    BOOLEAN present = FALSE;

    WdfWaitLockAcquire(DeviceContext->BridgeLock, NULL);
    present = BridgeTryPopInputLocked(DeviceContext, Report);
    WdfWaitLockRelease(DeviceContext->BridgeLock);
    return present;
}

BOOLEAN
BridgeTryPopInputForId(
    _In_ PDEVICE_CONTEXT DeviceContext,
    _In_ UCHAR ReportId,
    _Out_writes_(MIRABOX_CODEX_MICRO_REPORT_LENGTH) UCHAR *Report
    )
{
    BOOLEAN present = FALSE;
    ULONG offset;
    ULONG index;
    ULONG destination;
    ULONG source;

    WdfWaitLockAcquire(DeviceContext->BridgeLock, NULL);
    for (offset = 0; offset < DeviceContext->InputCount; offset += 1) {
        index = (DeviceContext->InputHead + offset) % MIRABOX_INPUT_RING_CAPACITY;
        if (DeviceContext->InputRing[index][0] != ReportId) {
            continue;
        }

        RtlCopyMemory(
            Report,
            DeviceContext->InputRing[index],
            MIRABOX_CODEX_MICRO_REPORT_LENGTH);
        // Remove the matching entry without disturbing the relative order of
        // reports for the other TLC. GET_INPUT_REPORT carries an explicit
        // report ID; consuming the oldest unrelated report would misroute it.
        for (; offset + 1 < DeviceContext->InputCount; offset += 1) {
            destination = (DeviceContext->InputHead + offset) %
                          MIRABOX_INPUT_RING_CAPACITY;
            source = (DeviceContext->InputHead + offset + 1) %
                     MIRABOX_INPUT_RING_CAPACITY;
            RtlCopyMemory(
                DeviceContext->InputRing[destination],
                DeviceContext->InputRing[source],
                MIRABOX_CODEX_MICRO_REPORT_LENGTH);
        }
        DeviceContext->InputCount -= 1;
        present = TRUE;
        break;
    }
    WdfWaitLockRelease(DeviceContext->BridgeLock);
    return present;
}

static BOOLEAN
BridgeTryPopOutputLocked(
    _In_ PDEVICE_CONTEXT DeviceContext,
    _Out_writes_(MIRABOX_CODEX_MICRO_REPORT_LENGTH) UCHAR *Report
    )
{
    BOOLEAN present = FALSE;

    if (DeviceContext->OutputCount != 0) {
        RtlCopyMemory(
            Report,
            DeviceContext->OutputRing[DeviceContext->OutputHead],
            MIRABOX_CODEX_MICRO_REPORT_LENGTH);
        DeviceContext->OutputHead =
            (DeviceContext->OutputHead + 1) % MIRABOX_OUTPUT_RING_CAPACITY;
        DeviceContext->OutputCount -= 1;
        present = TRUE;
    }
    return present;
}

static VOID
BridgeStoreOutputLocked(
    _In_ PDEVICE_CONTEXT DeviceContext,
    _In_reads_(MIRABOX_CODEX_MICRO_REPORT_LENGTH) const UCHAR *Report
    )
{
    ULONG slot;

    if (DeviceContext->OutputCount == MIRABOX_OUTPUT_RING_CAPACITY) {
        DeviceContext->OutputHead =
            (DeviceContext->OutputHead + 1) % MIRABOX_OUTPUT_RING_CAPACITY;
        DeviceContext->OutputCount -= 1;
    }
    slot = (DeviceContext->OutputHead + DeviceContext->OutputCount) %
           MIRABOX_OUTPUT_RING_CAPACITY;
    RtlCopyMemory(
        DeviceContext->OutputRing[slot],
        Report,
        MIRABOX_CODEX_MICRO_REPORT_LENGTH);
    DeviceContext->OutputCount += 1;
}

NTSTATUS
BridgeQueueCreate(
    _In_ WDFDEVICE Device,
    _Out_ WDFQUEUE *Queue
    )
{
    WDF_IO_QUEUE_CONFIG queueConfig;
    WDF_OBJECT_ATTRIBUTES queueAttributes;

    WDF_IO_QUEUE_CONFIG_INIT(&queueConfig, WdfIoQueueDispatchManual);
    WDF_OBJECT_ATTRIBUTES_INIT(&queueAttributes);
    queueAttributes.ParentObject = Device;
    return WdfIoQueueCreate(
        Device, &queueConfig, &queueAttributes, Queue);
}

NTSTATUS
BridgePushInput(
    _In_ PDEVICE_CONTEXT DeviceContext,
    _In_ WDFREQUEST Request
    )
{
    UCHAR report[MIRABOX_CODEX_MICRO_REPORT_LENGTH];
    NTSTATUS status;

    status = CopyInputReport(Request, report);
    if (!NT_SUCCESS(status)) {
        return status;
    }

    // The legacy custom IOCTL contract only injects primary Codex reports.
    if (report[0] != MIRABOX_CODEX_MICRO_REPORT_ID) {
        return STATUS_INVALID_PARAMETER;
    }

    status = BridgePushInputReport(DeviceContext, report);
    if (!NT_SUCCESS(status)) {
        return status;
    }

    // PUSH_INPUT has no output buffer.  IoStatus.Information is the number of
    // bytes returned to DeviceIoControl, not the number of input bytes
    // consumed; keep it at zero so METHOD_BUFFERED never advertises an
    // impossible 64-byte copy into a null output buffer.
    WdfRequestSetInformation(Request, 0);
    return STATUS_SUCCESS;
}

NTSTATUS
BridgePushInputReport(
    _In_ PDEVICE_CONTEXT DeviceContext,
    _In_reads_(MIRABOX_CODEX_MICRO_REPORT_LENGTH) const UCHAR *Report
    )
{
    WDFREQUEST readRequest = NULL;
    NTSTATUS copyStatus;
    NTSTATUS status;

    if (DeviceContext == NULL || Report == NULL ||
        (Report[0] != MIRABOX_CODEX_MICRO_REPORT_ID &&
         Report[0] != COMPANION_COLLECTION_REPORT_ID)) {
        return STATUS_INVALID_PARAMETER;
    }

    // Keep RESET, ManualQueue retrieval, and ring insertion in one critical
    // section. Otherwise ReadReport can check an empty ring just before this
    // function sees an empty ManualQueue, producing a report that is buffered
    // behind a read request that will never be completed.
    WdfWaitLockAcquire(DeviceContext->BridgeLock, NULL);
    if (DeviceContext->BridgeResetting) {
        WdfWaitLockRelease(DeviceContext->BridgeLock);
        return STATUS_CANCELLED;
    }

    status = WdfIoQueueRetrieveNextRequest(
        DeviceContext->ManualQueue, &readRequest);
    if (!NT_SUCCESS(status)) {
        BridgeStoreInputLocked(DeviceContext, Report);
        WdfWaitLockRelease(DeviceContext->BridgeLock);
        return STATUS_SUCCESS;
    }

    // Validate/copy while the request is still owned by us. If the HID
    // request is malformed, retain the report so the next valid read can
    // still observe it instead of silently dropping an input event.
    copyStatus = CompleteReportRequest(readRequest, Report);
    if (!NT_SUCCESS(copyStatus)) {
        BridgeStoreInputLocked(DeviceContext, Report);
    }
    WdfWaitLockRelease(DeviceContext->BridgeLock);
    WdfRequestComplete(readRequest, copyStatus);
    // The report was either delivered or retained for the next valid read;
    // do not make the producer retry and duplicate it.
    return STATUS_SUCCESS;
}

NTSTATUS
BridgeReadInput(
    _In_ PDEVICE_CONTEXT DeviceContext,
    _In_ WDFREQUEST Request,
    _Out_ BOOLEAN *CompleteRequest
    )
{
    UCHAR report[MIRABOX_CODEX_MICRO_REPORT_LENGTH];
    WDFMEMORY memory;
    size_t length;
    NTSTATUS status;

    *CompleteRequest = TRUE;

    // Check the destination before consuming a buffered report.  A malformed
    // HID request should not discard the next real input event.
    status = WdfRequestRetrieveOutputMemory(Request, &memory);
    if (!NT_SUCCESS(status)) {
        return status;
    }
    WdfMemoryGetBuffer(memory, &length);
    if (length < MIRABOX_CODEX_MICRO_REPORT_LENGTH) {
        return STATUS_BUFFER_TOO_SMALL;
    }

    WdfWaitLockAcquire(DeviceContext->BridgeLock, NULL);
    if (DeviceContext->BridgeResetting) {
        WdfWaitLockRelease(DeviceContext->BridgeLock);
        return STATUS_CANCELLED;
    }

    if (BridgeTryPopInputLocked(DeviceContext, report)) {
        WdfWaitLockRelease(DeviceContext->BridgeLock);
        status = CompleteReportRequest(Request, report);
        if (!NT_SUCCESS(status)) {
            // The memory was validated above, but preserve the event if a
            // concurrent teardown invalidates the request before completion.
            WdfWaitLockAcquire(DeviceContext->BridgeLock, NULL);
            if (!DeviceContext->BridgeResetting) {
                BridgeStoreInputLocked(DeviceContext, report);
            }
            WdfWaitLockRelease(DeviceContext->BridgeLock);
        }
        return status;
    }

    // The ring check and queue forward are intentionally under the same lock
    // as BridgePushInput's retrieve/store path; this is the lost-wakeup fix.
    status = WdfRequestForwardToIoQueue(
        Request, DeviceContext->ManualQueue);
    if (NT_SUCCESS(status)) {
        *CompleteRequest = FALSE;
    }
    WdfWaitLockRelease(DeviceContext->BridgeLock);
    return status;
}

NTSTATUS
BridgeReadOutput(
    _In_ PDEVICE_CONTEXT DeviceContext,
    _In_ WDFREQUEST Request,
    _Out_ BOOLEAN *CompleteRequest
    )
{
    UCHAR report[MIRABOX_CODEX_MICRO_REPORT_LENGTH];
    WDFMEMORY memory;
    size_t length;
    NTSTATUS status;

    *CompleteRequest = TRUE;
    status = WdfRequestRetrieveOutputMemory(Request, &memory);
    if (!NT_SUCCESS(status)) {
        return status;
    }
    WdfMemoryGetBuffer(memory, &length);
    if (length < MIRABOX_CODEX_MICRO_REPORT_LENGTH) {
        return STATUS_BUFFER_TOO_SMALL;
    }

    // Check, dequeue, and park the request under the same lock used by RESET.
    // This makes RESET a real barrier: a request cannot slip into the manual
    // queue after RESET has drained it.
    WdfWaitLockAcquire(DeviceContext->BridgeLock, NULL);
    if (DeviceContext->BridgeResetting) {
        WdfWaitLockRelease(DeviceContext->BridgeLock);
        return STATUS_CANCELLED;
    }

    if (BridgeTryPopOutputLocked(DeviceContext, report)) {
        WdfWaitLockRelease(DeviceContext->BridgeLock);
        return CompleteReportRequest(Request, report);
    }

    status = WdfRequestForwardToIoQueue(Request, DeviceContext->BridgeOutputQueue);
    if (NT_SUCCESS(status)) {
        *CompleteRequest = FALSE;
    }
    WdfWaitLockRelease(DeviceContext->BridgeLock);
    return status;
}

NTSTATUS
BridgePublishOutput(
    _In_ PDEVICE_CONTEXT DeviceContext,
    _In_reads_(MIRABOX_CODEX_MICRO_REPORT_LENGTH) const UCHAR *Report
    )
{
    UCHAR companionReport[MIRABOX_CODEX_MICRO_REPORT_LENGTH];
    NTSTATUS status;

    if (DeviceContext == NULL || Report == NULL ||
        Report[0] != MIRABOX_CODEX_MICRO_REPORT_ID) {
        return STATUS_INVALID_PARAMETER;
    }
    RtlCopyMemory(companionReport, Report, MIRABOX_CODEX_MICRO_REPORT_LENGTH);
    companionReport[0] = COMPANION_COLLECTION_REPORT_ID;

    // HID_READ_REPORT has no report-ID selector.  Put the translated ID-7
    // report in the same input path as ID-6 events; HIDClass dispatches the
    // completed report to the matching top-level collection.
    status = BridgePushInputReport(DeviceContext, companionReport);
    if (!NT_SUCCESS(status)) {
        return status;
    }

    // Keep the legacy custom-IOCTL copy independently available for
    // diagnostic clients.  It is deliberately published after the common HID
    // path so a failed provisional reader cannot block Codex output delivery.
    WdfWaitLockAcquire(DeviceContext->BridgeLock, NULL);
    if (DeviceContext->BridgeResetting) {
        WdfWaitLockRelease(DeviceContext->BridgeLock);
        return STATUS_CANCELLED;
    }

    {
        WDFREQUEST sidebandRequest = NULL;
        status = WdfIoQueueRetrieveNextRequest(
            DeviceContext->BridgeOutputQueue, &sidebandRequest);
        if (!NT_SUCCESS(status)) {
            BridgeStoreOutputLocked(DeviceContext, Report);
        }
        WdfWaitLockRelease(DeviceContext->BridgeLock);
        if (sidebandRequest != NULL) {
            status = CompleteReportRequest(sidebandRequest, Report);
            if (!NT_SUCCESS(status)) {
                WdfWaitLockAcquire(DeviceContext->BridgeLock, NULL);
                if (!DeviceContext->BridgeResetting) {
                    BridgeStoreOutputLocked(DeviceContext, Report);
                }
                WdfWaitLockRelease(DeviceContext->BridgeLock);
            }
            WdfRequestComplete(sidebandRequest, status);
        }
    }
    return STATUS_SUCCESS;
}

NTSTATUS
BridgeCopyHidPacket(
    _In_ const HID_XFER_PACKET *Packet,
    _Out_writes_(MIRABOX_CODEX_MICRO_REPORT_LENGTH) UCHAR *Report
    )
{
    return BridgeCopyHidPacketForId(
        Packet, MIRABOX_CODEX_MICRO_REPORT_ID, Report);
}

NTSTATUS
BridgeCopyHidPacketForId(
    _In_ const HID_XFER_PACKET *Packet,
    _In_ UCHAR ExpectedReportId,
    _Out_writes_(MIRABOX_CODEX_MICRO_REPORT_LENGTH) UCHAR *Report
    )
{
    if (Packet == NULL || Report == NULL ||
        Packet->reportId != ExpectedReportId) {
        return STATUS_INVALID_PARAMETER;
    }
    if (Packet->reportBuffer == NULL ||
        (Packet->reportBufferLen != MIRABOX_CODEX_MICRO_BODY_LENGTH &&
         Packet->reportBufferLen != MIRABOX_CODEX_MICRO_REPORT_LENGTH)) {
        return STATUS_INVALID_BUFFER_SIZE;
    }

    RtlZeroMemory(Report, MIRABOX_CODEX_MICRO_REPORT_LENGTH);
    if (Packet->reportBufferLen == MIRABOX_CODEX_MICRO_REPORT_LENGTH) {
        // UMDF's translated HID request normally carries the full report in
        // the input buffer, with the ID repeated in the side-band field.  Do
        // not silently reinterpret a malformed 64-byte packet as a 63-byte
        // body: that would drop the final byte and hide a framing bug.
        if (Packet->reportBuffer[0] != ExpectedReportId) {
            return STATUS_INVALID_PARAMETER;
        }
        RtlCopyMemory(
            Report,
            Packet->reportBuffer,
            MIRABOX_CODEX_MICRO_REPORT_LENGTH);
    } else {
        Report[0] = ExpectedReportId;
        RtlCopyMemory(
            Report + 1,
            Packet->reportBuffer,
            MIRABOX_CODEX_MICRO_BODY_LENGTH);
    }
    return STATUS_SUCCESS;
}

NTSTATUS
BridgeGetInfo(
    _In_ PDEVICE_CONTEXT DeviceContext,
    _In_ WDFREQUEST Request
    )
{
    MIRABOX_CODEX_INFO info;
    UNREFERENCED_PARAMETER(DeviceContext);

    RtlZeroMemory(&info, sizeof(info));
    info.VendorId = MIRABOX_CODEX_MICRO_VID;
    info.ProductId = MIRABOX_CODEX_MICRO_PID;
    info.Release = MIRABOX_CODEX_MICRO_RELEASE;
    info.UsagePage = MIRABOX_CODEX_MICRO_USAGE_PAGE;
    info.ReportId = MIRABOX_CODEX_MICRO_REPORT_ID;
    info.ReportLength = MIRABOX_CODEX_MICRO_REPORT_LENGTH;
    return RequestCopyFromBuffer(Request, &info, sizeof(info));
}

NTSTATUS
BridgeReset(
    _In_ PDEVICE_CONTEXT DeviceContext,
    _In_ WDFREQUEST Request
    )
{
    WDFREQUEST request = NULL;
    NTSTATUS status;

    UNREFERENCED_PARAMETER(Request);

    // Establish a barrier before clearing either ring.  BridgeReadOutput
    // checks this flag while holding BridgeLock, so no new request can be
    // forwarded after the drain begins.
    WdfWaitLockAcquire(DeviceContext->BridgeLock, NULL);
    DeviceContext->BridgeResetting = TRUE;
    DeviceContext->InputHead = 0;
    DeviceContext->InputCount = 0;
    DeviceContext->OutputHead = 0;
    DeviceContext->OutputCount = 0;
    RtlZeroMemory(DeviceContext->InputRing, sizeof(DeviceContext->InputRing));
    RtlZeroMemory(DeviceContext->OutputRing, sizeof(DeviceContext->OutputRing));
    WdfWaitLockRelease(DeviceContext->BridgeLock);

    // Complete all pending reads from both queues.  HID READ_REPORT requests
    // wait in ManualQueue, while the provisional custom-IOCTL READ_OUTPUT
    // requests wait in BridgeOutputQueue.  Leaving either queue populated
    // would make an overlapped reader wait forever even though RESET promised
    // an empty, fresh bridge state.
    for (;;) {
        status = WdfIoQueueRetrieveNextRequest(
            DeviceContext->ManualQueue, &request);
        if (!NT_SUCCESS(status)) {
            break;
        }
        WdfRequestComplete(request, STATUS_CANCELLED);
        request = NULL;
    }

    for (;;) {
        status = WdfIoQueueRetrieveNextRequest(
            DeviceContext->BridgeOutputQueue, &request);
        if (!NT_SUCCESS(status)) {
            break;
        }
        WdfRequestComplete(request, STATUS_CANCELLED);
        request = NULL;
    }

    WdfWaitLockAcquire(DeviceContext->BridgeLock, NULL);
    DeviceContext->BridgeResetting = FALSE;
    WdfWaitLockRelease(DeviceContext->BridgeLock);
    return STATUS_SUCCESS;
}
