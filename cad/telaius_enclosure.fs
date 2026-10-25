FeatureScript 3095;
import(path : "onshape/std/common.fs", version : "3095.0");

// Enclosure for the Telaius modem, the DFRobot TEL0162 board (SIM7600G-H, 60 x 52 mm): two U-shaped halves that fit
// together without screws, built by one feature from one set of sizes so they always match. The parts are laid out
// either for printing (side by side on the XY plane, the lid upside down so its top is on the bed) or assembled; both
// layouts are centred on the origin.
//
// Base: the floor with the two long side walls. The board sits on two standoffs whose pins go through its two 3.1 mm
// holes, and on an L-shaped stop across the connector end: the board's edge lies on its ledge and butts against its
// upright, which takes the load of plugging in cables. Four clips on the side walls hold the board down.
//
// Lid: the top with the front (connector) and back walls. The end walls drop in between the base's side walls into
// rabbets at both ends of the floor, and grooves in their edges snap over ridges on the side walls. The only opening
// is for the USB-C plug. Under the top hang the antenna holders: a slot along the back wall for the flat GSM antenna,
// standing on its long edge, and in front of it a well for the square ceramic GPS patch, which lies against the top.
// The slot's front wall is only as wide as the well, so the antenna's cable can leave from either end.
// The GSM antenna goes at the back because at the front it would cover the USB-C opening and hit the connectors.
//
// Coordinates follow the board seen from the top: x along the 60 mm side from the connector edge, y along the 52 mm
// side from the antenna-connector edge, z up from the bottom of the floor.
export enum EnclosureLayout
{
    annotation { "Name" : "Print (on the bed, lid upside down)" }
    PRINT,
    annotation { "Name" : "Assembled" }
    ASSEMBLED
}

annotation { "Feature Type Name" : "Telaius Enclosure", "Feature Type Description" : "Two-part snap-fit case for the TEL0162 modem board" }
export const telaiusEnclosure = defineFeature(function(context is Context, id is Id, definition is map)
    precondition
    {
        annotation { "Name" : "Make base", "Default" : true }
        definition.makeBase is boolean;
        annotation { "Name" : "Make lid", "Default" : true }
        definition.makeLid is boolean;
        annotation { "Name" : "Layout" }
        definition.layout is EnclosureLayout;
        if (definition.layout == EnclosureLayout.PRINT)
        {
            annotation { "Name" : "Gap between the parts" }
            isLength(definition.printGap, { (millimeter) : [0, 10, 200] } as LengthBoundSpec);
        }
        else
        {
            annotation { "Name" : "Lift the lid (to look inside)" }
            isLength(definition.lidLift, { (millimeter) : [0, 0, 200] } as LengthBoundSpec);
        }

        annotation { "Name" : "Board length (connector edge to far edge)" }
        isLength(definition.boardX, { (millimeter) : [10, 60, 300] } as LengthBoundSpec);
        annotation { "Name" : "Board width" }
        isLength(definition.boardY, { (millimeter) : [10, 52, 300] } as LengthBoundSpec);
        annotation { "Name" : "Board thickness" }
        isLength(definition.pcb, { (millimeter) : [0.4, 1.6, 5] } as LengthBoundSpec);
        annotation { "Name" : "Hole centre from far edge" }
        isLength(definition.holeFromEnd, { (millimeter) : [1, 3.5, 50] } as LengthBoundSpec);
        annotation { "Name" : "Hole centre from side edges" }
        isLength(definition.holeFromSide, { (millimeter) : [1, 3.5, 50] } as LengthBoundSpec);

        annotation { "Name" : "Wall thickness" }
        isLength(definition.wall, { (millimeter) : [0.8, 2, 10] } as LengthBoundSpec);
        annotation { "Name" : "Floor thickness" }
        isLength(definition.floor, { (millimeter) : [0.8, 2, 10] } as LengthBoundSpec);
        annotation { "Name" : "Inside height (floor to top of side walls)" }
        isLength(definition.height, { (millimeter) : [5, 16, 200] } as LengthBoundSpec);

        annotation { "Name" : "Gap board to side walls" }
        isLength(definition.sideGap, { (millimeter) : [0, 0.5, 20] } as LengthBoundSpec);
        annotation { "Name" : "Gap connector edge to stop" }
        isLength(definition.frontGap, { (millimeter) : [0, 0.1, 20] } as LengthBoundSpec);
        annotation { "Name" : "Gap far edge to back wall" }
        isLength(definition.backGap, { (millimeter) : [0, 2, 50] } as LengthBoundSpec);

        annotation { "Name" : "Lid end wall thickness (incl. fit clearance)" }
        isLength(definition.lidWall, { (millimeter) : [0.8, 2, 10] } as LengthBoundSpec);
        annotation { "Name" : "Fit clearance" }
        isLength(definition.fit, { (millimeter) : [0, 0.3, 2] } as LengthBoundSpec);
        annotation { "Name" : "Rabbet depth (lid walls sink into the floor)" }
        isLength(definition.rabbet, { (millimeter) : [0, 0.8, 5] } as LengthBoundSpec);

        annotation { "Name" : "Standoff height (floor to board)" }
        isLength(definition.standoffHeight, { (millimeter) : [2, 3.5, 30] } as LengthBoundSpec);
        annotation { "Name" : "Standoff diameter" }
        isLength(definition.standoffDiameter, { (millimeter) : [3, 6, 20] } as LengthBoundSpec);
        annotation { "Name" : "Pin diameter" }
        isLength(definition.pinDiameter, { (millimeter) : [1, 2.8, 10] } as LengthBoundSpec);
        annotation { "Name" : "Pin above the board" }
        isLength(definition.pinAbove, { (millimeter) : [0, 1, 20] } as LengthBoundSpec);
        annotation { "Name" : "Pin top chamfer" }
        isLength(definition.pinChamfer, { (millimeter) : [0, 0.4, 2] } as LengthBoundSpec);

        annotation { "Name" : "Stop ledge width (under the board)" }
        isLength(definition.ledgeWidth, { (millimeter) : [0.5, 1.4, 10] } as LengthBoundSpec);
        annotation { "Name" : "Stop upright thickness" }
        isLength(definition.stopThickness, { (millimeter) : [0.5, 1.5, 10] } as LengthBoundSpec);
        annotation { "Name" : "Stop upright above the board top" }
        isLength(definition.stopAbove, { (millimeter) : [-1.5, 0, 10] } as LengthBoundSpec);

        annotation { "Name" : "USB-C centre from the antenna edge" }
        isLength(definition.usbCenter, { (millimeter) : [0, 19.85, 300] } as LengthBoundSpec);
        annotation { "Name" : "USB-C centre above the board top" }
        isLength(definition.usbAbove, { (millimeter) : [0, 1.6, 20] } as LengthBoundSpec);
        annotation { "Name" : "USB-C plug opening width" }
        isLength(definition.usbWidth, { (millimeter) : [1, 13, 50] } as LengthBoundSpec);
        annotation { "Name" : "USB-C plug opening height" }
        isLength(definition.usbHeight, { (millimeter) : [1, 7.5, 50] } as LengthBoundSpec);

        annotation { "Name" : "Board clip 1 from the connector edge" }
        isLength(definition.clipX1, { (millimeter) : [0, 15, 300] } as LengthBoundSpec);
        annotation { "Name" : "Board clip 2 from the connector edge" }
        isLength(definition.clipX2, { (millimeter) : [0, 42, 300] } as LengthBoundSpec);
        annotation { "Name" : "Board clip length" }
        isLength(definition.clipLength, { (millimeter) : [1, 5, 50] } as LengthBoundSpec);
        annotation { "Name" : "Board clip overlap over the board edge" }
        isLength(definition.clipOverlap, { (millimeter) : [0, 0.3, 3] } as LengthBoundSpec);
        annotation { "Name" : "Board clip radius" }
        isLength(definition.clipRadius, { (millimeter) : [0.3, 1, 5] } as LengthBoundSpec);
        annotation { "Name" : "Board clip clearance above the board" }
        isLength(definition.clipClearance, { (millimeter) : [0, 0.05, 2] } as LengthBoundSpec);

        annotation { "Name" : "Snap ridge depth" }
        isLength(definition.snapDepth, { (millimeter) : [0, 0.5, 3] } as LengthBoundSpec);
        annotation { "Name" : "Snap ridge radius" }
        isLength(definition.snapRadius, { (millimeter) : [0.3, 1, 5] } as LengthBoundSpec);
        annotation { "Name" : "Snap ridge below top of walls" }
        isLength(definition.snapFromTop, { (millimeter) : [1, 4, 100] } as LengthBoundSpec);
        annotation { "Name" : "Snap groove clearance" }
        isLength(definition.grooveClearance, { (millimeter) : [0, 0.1, 1] } as LengthBoundSpec);

        annotation { "Name" : "Lid top thickness" }
        isLength(definition.top, { (millimeter) : [0.8, 2, 10] } as LengthBoundSpec);
        annotation { "Name" : "Lid end walls clear of the rabbet bottom" }
        isLength(definition.lidGapBelow, { (millimeter) : [0, 0.2, 2] } as LengthBoundSpec);

        annotation { "Name" : "GSM antenna slot width (antenna thickness)" }
        isLength(definition.gsmSlot, { (millimeter) : [0.3, 1.5, 10] } as LengthBoundSpec);
        annotation { "Name" : "GSM slot wall depth (from the top)" }
        isLength(definition.gsmSlotDepth, { (millimeter) : [1, 8, 100] } as LengthBoundSpec);
        annotation { "Name" : "GPS patch size (square)" }
        isLength(definition.gpsSize, { (millimeter) : [5, 25, 100] } as LengthBoundSpec);
        annotation { "Name" : "GPS patch thickness" }
        isLength(definition.gpsThickness, { (millimeter) : [1, 6, 50] } as LengthBoundSpec);
        annotation { "Name" : "GPS well clearance" }
        isLength(definition.gpsClearance, { (millimeter) : [0, 0.4, 3] } as LengthBoundSpec);
        annotation { "Name" : "Antenna holder wall thickness" }
        isLength(definition.holderWall, { (millimeter) : [0.6, 1.2, 5] } as LengthBoundSpec);
    }
    {
        const mm = millimeter;
        const bx = definition.boardX;
        const by = definition.boardY;
        const zf = definition.floor;
        const zt = zf + definition.height; // top of the side walls, underside of the lid's top
        const zb = zf + definition.standoffHeight; // underside of the board
        const zBoardTop = zb + definition.pcb;

        if (!definition.makeBase && !definition.makeLid)
            throw regenError("Nothing to make", ["makeBase", "makeLid"]);
        if (definition.rabbet >= zf)
            throw regenError("Rabbet must be shallower than the floor", ["rabbet"]);
        if (zBoardTop > zt)
            throw regenError("Board does not fit under the top of the walls", ["height"]);
        if (definition.snapRadius <= definition.snapDepth || definition.snapRadius - definition.snapDepth >= definition.wall)
            throw regenError("Snap ridge radius must exceed its depth and stay inside the wall", ["snapRadius"]);
        if (definition.snapFromTop + definition.snapRadius >= definition.height)
            throw regenError("Snap ridge is too low", ["snapFromTop"]);
        if (definition.pinDiameter >= definition.standoffDiameter)
            throw regenError("Pin must be thinner than the standoff", ["pinDiameter"]);
        const clipReach = definition.sideGap + definition.clipOverlap; // how far a clip sticks out of its wall
        if (clipReach >= definition.clipRadius || definition.clipRadius - clipReach >= definition.wall)
            throw regenError("Board clip radius must exceed side gap + overlap and stay inside the wall", ["clipRadius"]);
        if (definition.fit >= definition.lidWall)
            throw regenError("Fit clearance must be thinner than the lid's end walls", ["fit"]);

        // Outline: x from the outer face of the lid's front wall (outside the stop's upright) to the outer face of its
        // back wall
        const xs0 = -definition.frontGap - definition.stopThickness; // outer face of the stop's upright
        const xa = xs0 - definition.lidWall;
        const xBack = bx + definition.backGap; // inner face of the lid's back wall
        const xb = xBack + definition.lidWall;
        const yi0 = -definition.sideGap;
        const yi1 = by + definition.sideGap;
        const yo0 = yi0 - definition.wall;
        const yo1 = yi1 + definition.wall;

        // Snap ridges on the side walls and the matching grooves in the lid's end walls share these axes (along x)
        const zs = zt - definition.snapFromTop;
        const snapYs = [yi0 + definition.snapDepth - definition.snapRadius, yi1 - definition.snapDepth + definition.snapRadius];
        const snapSpans = [[xa, xs0], [xBack - definition.fit, xb]];

        // USB-C plug: the notch in the stop's upright and the opening in the lid's front wall
        const zUsb = zBoardTop + definition.usbAbove;
        const usbY0 = definition.usbCenter - definition.usbWidth / 2;
        const usbY1 = definition.usbCenter + definition.usbWidth / 2;
        const usbZ0 = zUsb - definition.usbHeight / 2;
        const usbZ1 = zUsb + definition.usbHeight / 2;

        if (definition.makeBase)
        {
            // Floor and side walls: a block minus the space between the walls, open at both ends
            fCuboid(context, id + "outer", { "corner1" : vector(xa, yo0, 0 * mm), "corner2" : vector(xb, yo1, zt) });
            const base = qCreatedBy(id + "outer", EntityType.BODY);
            fCuboid(context, id + "inner", { "corner1" : vector(xa - mm, yi0, zf), "corner2" : vector(xb + mm, yi1, zt + mm) });
            var cuts = [qCreatedBy(id + "inner", EntityType.BODY)];

            // Rabbets at both ends: the lid's end walls stand in them, which keeps the lid from sliding along x
            if (definition.rabbet > 0 * mm)
            {
                fCuboid(context, id + "rabbetFront", {
                            "corner1" : vector(xa - mm, yi0, zf - definition.rabbet),
                            "corner2" : vector(xs0, yi1, zf + mm) });
                fCuboid(context, id + "rabbetBack", {
                            "corner1" : vector(xBack - definition.fit, yi0, zf - definition.rabbet),
                            "corner2" : vector(xb + mm, yi1, zf + mm) });
                cuts = concatenateArrays([cuts, [qCreatedBy(id + "rabbetFront", EntityType.BODY),
                                qCreatedBy(id + "rabbetBack", EntityType.BODY)]]);
            }
            opBoolean(context, id + "hollow", {
                        "tools" : qUnion(cuts),
                        "targets" : base,
                        "operationType" : BooleanOperationType.SUBTRACTION });

            var adds = [];

            // Standoffs at the two holes, each tied to its side wall by a web, with a pin through the board
            const hx = bx - definition.holeFromEnd;
            const rs = definition.standoffDiameter / 2;
            const rp = definition.pinDiameter / 2;
            const zPin = zBoardTop + definition.pinAbove;
            const holes = [definition.holeFromSide, by - definition.holeFromSide];
            for (var i = 0; i < 2; i += 1)
            {
                const hy = holes[i];
                const wallFace = i == 0 ? yi0 - 0.5 * mm : yi1 + 0.5 * mm;
                fCylinder(context, id + ("standoff" ~ i), {
                            "bottomCenter" : vector(hx, hy, zf - 0.5 * mm),
                            "topCenter" : vector(hx, hy, zb),
                            "radius" : rs });
                fCuboid(context, id + ("web" ~ i), {
                            "corner1" : vector(hx - rs, wallFace, zf - 0.5 * mm),
                            "corner2" : vector(hx + rs, hy, zb) });
                fCylinder(context, id + ("pin" ~ i), {
                            "bottomCenter" : vector(hx, hy, zb - 0.5 * mm),
                            "topCenter" : vector(hx, hy, zPin),
                            "radius" : rp });
                if (definition.pinChamfer > 0 * mm)
                {
                    opChamfer(context, id + ("pinChamfer" ~ i), {
                                "entities" : qContainsPoint(qCreatedBy(id + ("pin" ~ i), EntityType.EDGE), vector(hx + rp, hy, zPin)),
                                "chamferType" : ChamferType.EQUAL_OFFSETS,
                                "width" : definition.pinChamfer });
                }
                adds = concatenateArrays([adds, [qCreatedBy(id + ("standoff" ~ i), EntityType.BODY),
                                qCreatedBy(id + ("web" ~ i), EntityType.BODY), qCreatedBy(id + ("pin" ~ i), EntityType.BODY)]]);
            }

            // L-shaped stop across the connector end, wall to wall: a ledge under the board's edge and an upright in
            // front of it, level with the board top so it stays below the connectors. A notch in the upright makes room
            // for the USB-C plug's moulding, which reaches below the board top.
            const zStop = zBoardTop + definition.stopAbove;
            fCuboid(context, id + "stopUpright", {
                        "corner1" : vector(xs0, yi0 - 0.5 * mm, zf - 0.5 * mm),
                        "corner2" : vector(-definition.frontGap, yi1 + 0.5 * mm, zStop) });
            fCuboid(context, id + "stopLedge", {
                        "corner1" : vector(-definition.frontGap - 0.5 * mm, yi0 - 0.5 * mm, zf - 0.5 * mm),
                        "corner2" : vector(definition.ledgeWidth, yi1 + 0.5 * mm, zb) });
            const upright = qCreatedBy(id + "stopUpright", EntityType.BODY);
            if (usbZ0 < zStop)
            {
                fCuboid(context, id + "usbNotch", {
                            "corner1" : vector(xs0 - mm, usbY0, usbZ0),
                            "corner2" : vector(-definition.frontGap + 0.01 * mm, usbY1, zStop + mm) });
                opBoolean(context, id + "usbNotchCut", {
                            "tools" : qCreatedBy(id + "usbNotch", EntityType.BODY),
                            "targets" : upright,
                            "operationType" : BooleanOperationType.SUBTRACTION });
            }
            adds = concatenateArrays([adds, [upright, qCreatedBy(id + "stopLedge", EntityType.BODY)]]);

            // Board clips: short half-round bumps on the side walls just above the board top, reaching clipOverlap over
            // its edge; the board is pressed down past them. The height puts the round's underside, where it overhangs
            // the board edge, clipClearance above the board.
            const rc = definition.clipRadius;
            const zClip = zBoardTop + definition.clipClearance + sqrt(rc ^ 2 - (rc - definition.clipOverlap) ^ 2);
            const clipYs = [yi0 + clipReach - rc, yi1 - clipReach + rc];
            const clipXs = [definition.clipX1, definition.clipX2];
            for (var c = 0; c < 2; c += 1)
            {
                for (var side = 0; side < 2; side += 1)
                {
                    const name = "clip" ~ c ~ side;
                    fCylinder(context, id + name, {
                                "bottomCenter" : vector(clipXs[c] - definition.clipLength / 2, clipYs[side], zClip),
                                "topCenter" : vector(clipXs[c] + definition.clipLength / 2, clipYs[side], zClip),
                                "radius" : rc });
                    adds = append(adds, qCreatedBy(id + name, EntityType.BODY));
                }
            }

            // Snap ridges: half-round ridges on the inside of the side walls where the lid's end walls come down
            if (definition.snapDepth > 0 * mm)
            {
                for (var e = 0; e < 2; e += 1)
                {
                    for (var s = 0; s < 2; s += 1)
                    {
                        const name = "snap" ~ e ~ s;
                        fCylinder(context, id + name, {
                                    "bottomCenter" : vector(snapSpans[e][0], snapYs[s], zs),
                                    "topCenter" : vector(snapSpans[e][1], snapYs[s], zs),
                                    "radius" : definition.snapRadius });
                        adds = append(adds, qCreatedBy(id + name, EntityType.BODY));
                    }
                }
            }

            opBoolean(context, id + "join", {
                        "tools" : qUnion(concatenateArrays([[base], adds])),
                        "operationType" : BooleanOperationType.UNION });
        }

        if (definition.makeLid)
        {
            // Top over the whole base, resting on the side walls
            fCuboid(context, id + "lidTop", {
                        "corner1" : vector(xa, yo0, zt),
                        "corner2" : vector(xb, yo1, zt + definition.top) });
            const lid = qCreatedBy(id + "lidTop", EntityType.BODY);
            var parts = [];

            // End walls: between the side walls with fit/2 each side, down into the rabbets but clear of their bottom;
            // each is fit thinner than its rabbet on the inside
            const zWall0 = zf - definition.rabbet + definition.lidGapBelow;
            const ends = [[xa, xs0 - definition.fit], [xBack, xb]];
            for (var e = 0; e < 2; e += 1)
            {
                fCuboid(context, id + ("lidEnd" ~ e), {
                            "corner1" : vector(ends[e][0], yi0 + definition.fit / 2, zWall0),
                            "corner2" : vector(ends[e][1], yi1 - definition.fit / 2, zt + 0.01 * mm) });
                parts = append(parts, qCreatedBy(id + ("lidEnd" ~ e), EntityType.BODY));
            }

            // Antenna holders under the top. The GSM slot runs along the back wall, between it and a wall that is also
            // the back of the GPS well and only as wide as the well (the GSM antenna's cable leaves past its ends); the
            // GPS well holds the patch flat against the top.
            const t = definition.holderWall;
            const well = definition.gpsSize + definition.gpsClearance; // inside of the well
            const xSlot = xBack - definition.gsmSlot; // front face of the slot
            const zSlot = zt - definition.gsmSlotDepth;
            if (xSlot - t < bx && zSlot < zBoardTop + 0.5 * mm)
                throw regenError("GSM slot wall reaches the board: make it shallower or the back gap larger", ["gsmSlotDepth"]);
            fCuboid(context, id + "gsmWall", {
                        "corner1" : vector(xSlot - t, by / 2 - well / 2 - t, zSlot),
                        "corner2" : vector(xSlot, by / 2 + well / 2 + t, zt + 0.01 * mm) });
            parts = append(parts, qCreatedBy(id + "gsmWall", EntityType.BODY));

            const zWell = zt - definition.gpsThickness - definition.gpsClearance;
            const wx1 = xSlot - t; // back of the well's inside
            const wx0 = wx1 - well;
            const wy0 = by / 2 - well / 2;
            const wy1 = by / 2 + well / 2;
            fCuboid(context, id + "gpsOuter", {
                        "corner1" : vector(wx0 - t, wy0 - t, zWell),
                        "corner2" : vector(wx1 + 0.01 * mm, wy1 + t, zt + 0.01 * mm) });
            fCuboid(context, id + "gpsInner", {
                        "corner1" : vector(wx0, wy0, zWell - mm),
                        "corner2" : vector(wx1, wy1, zt) });
            opBoolean(context, id + "gpsHollow", {
                        "tools" : qCreatedBy(id + "gpsInner", EntityType.BODY),
                        "targets" : qCreatedBy(id + "gpsOuter", EntityType.BODY),
                        "operationType" : BooleanOperationType.SUBTRACTION });
            parts = append(parts, qCreatedBy(id + "gpsOuter", EntityType.BODY));

            opBoolean(context, id + "lidJoin", {
                        "tools" : qUnion(concatenateArrays([[lid], parts])),
                        "operationType" : BooleanOperationType.UNION });

            // Grooves for the snap ridges and the USB-C opening
            var lidCuts = [];
            if (definition.snapDepth > 0 * mm)
            {
                for (var g = 0; g < 2; g += 1)
                {
                    for (var s = 0; s < 2; s += 1)
                    {
                        const name = "groove" ~ g ~ s;
                        fCylinder(context, id + name, {
                                    "bottomCenter" : vector(ends[g][0] - mm, snapYs[s], zs),
                                    "topCenter" : vector(ends[g][1] + mm, snapYs[s], zs),
                                    "radius" : definition.snapRadius + definition.grooveClearance });
                        lidCuts = append(lidCuts, qCreatedBy(id + name, EntityType.BODY));
                    }
                }
            }
            fCuboid(context, id + "usbOpening", {
                        "corner1" : vector(xa - mm, usbY0, usbZ0),
                        "corner2" : vector(xs0 - definition.fit + mm, usbY1, usbZ1) });
            lidCuts = append(lidCuts, qCreatedBy(id + "usbOpening", EntityType.BODY));
            opBoolean(context, id + "lidCut", {
                        "tools" : qUnion(lidCuts),
                        "targets" : lid,
                        "operationType" : BooleanOperationType.SUBTRACTION });
        }

        // Layout. The parts were built assembled with the board's corner at the origin; move them so the layout is
        // centred on the origin. For printing the lid is turned over about the x axis, top down on the XY plane, and
        // the parts sit side by side along y.
        const cx = (xa + xb) / 2;
        const cy = (yo0 + yo1) / 2;
        const baseBody = qCreatedBy(id + "outer", EntityType.BODY);
        const lidBody = qCreatedBy(id + "lidTop", EntityType.BODY);
        if (definition.layout == EnclosureLayout.PRINT)
        {
            const both = definition.makeBase && definition.makeLid;
            const half = (yo1 - yo0) / 2 + definition.printGap / 2;
            if (definition.makeBase)
            {
                opTransform(context, id + "placeBase", {
                            "bodies" : baseBody,
                            "transform" : transform(vector(-cx, (both ? -half : 0 * mm) - cy, 0 * mm)) });
            }
            if (definition.makeLid)
            {
                // Turned over about the line along x at y = cy, z = zt, the top's outer face goes to z = zt - top
                opTransform(context, id + "flipLid", {
                            "bodies" : lidBody,
                            "transform" : rotationAround(line(vector(cx, cy, zt), vector(1, 0, 0)), 180 * degree) });
                opTransform(context, id + "placeLid", {
                            "bodies" : lidBody,
                            "transform" : transform(vector(-cx, (both ? half : 0 * mm) - cy, definition.top - zt)) });
            }
        }
        else
        {
            if (definition.makeBase)
            {
                opTransform(context, id + "placeBase", {
                            "bodies" : baseBody,
                            "transform" : transform(vector(-cx, -cy, 0 * mm)) });
            }
            if (definition.makeLid)
            {
                opTransform(context, id + "placeLid", {
                            "bodies" : lidBody,
                            "transform" : transform(vector(-cx, -cy, definition.lidLift)) });
            }
        }
    }, {
        "makeBase" : true, "makeLid" : true, "layout" : EnclosureLayout.PRINT,
        "printGap" : 10 * millimeter, "lidLift" : 0 * millimeter,
        "boardX" : 60 * millimeter, "boardY" : 52 * millimeter, "pcb" : 1.6 * millimeter,
        "holeFromEnd" : 3.5 * millimeter, "holeFromSide" : 3.5 * millimeter,
        "wall" : 2 * millimeter, "floor" : 2 * millimeter, "height" : 16 * millimeter,
        "sideGap" : 0.5 * millimeter, "frontGap" : 0.1 * millimeter, "backGap" : 2 * millimeter,
        "lidWall" : 2 * millimeter, "fit" : 0.3 * millimeter, "rabbet" : 0.8 * millimeter,
        "standoffHeight" : 3.5 * millimeter, "standoffDiameter" : 6 * millimeter, "pinDiameter" : 2.8 * millimeter,
        "pinAbove" : 1 * millimeter, "pinChamfer" : 0.4 * millimeter,
        "ledgeWidth" : 1.4 * millimeter, "stopThickness" : 1.5 * millimeter, "stopAbove" : 0 * millimeter,
        "usbCenter" : 19.85 * millimeter, "usbAbove" : 1.6 * millimeter, "usbWidth" : 13 * millimeter,
        "usbHeight" : 7.5 * millimeter,
        "clipX1" : 15 * millimeter, "clipX2" : 42 * millimeter, "clipLength" : 5 * millimeter,
        "clipOverlap" : 0.3 * millimeter, "clipRadius" : 1 * millimeter, "clipClearance" : 0.05 * millimeter,
        "snapDepth" : 0.5 * millimeter, "snapRadius" : 1 * millimeter, "snapFromTop" : 4 * millimeter,
        "grooveClearance" : 0.1 * millimeter,
        "top" : 2 * millimeter, "lidGapBelow" : 0.2 * millimeter,
        "gsmSlot" : 1.5 * millimeter, "gsmSlotDepth" : 8 * millimeter,
        "gpsSize" : 25 * millimeter, "gpsThickness" : 6 * millimeter, "gpsClearance" : 0.4 * millimeter,
        "holderWall" : 1.2 * millimeter
    });
