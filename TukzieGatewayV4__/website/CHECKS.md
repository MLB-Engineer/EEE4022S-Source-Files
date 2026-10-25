# Change checklist and validation

## PDF notes implemented

| Requested area | Result |
| --- | --- |
| Login | Developer D; centred Tukzie mark and console title |
| Overview | Removed developer hero, battery-state widget, environmental widget, navigation, vehicle information, graphs, dynamics, range analytics and estimated trip; retained raw cards without field footnotes |
| Trips | Kept the two requested empty-state phrases; removed developer map placeholder and destination search; added a labelled illustrative UCT–CBD route preview |
| System Health | Disconnected state and available connection/storage/telemetry diagnostics |
| Control | Single Remote Control heading; square Hoot/Left/Right/Hazards buttons; sent/ack timestamps beneath each; on/off support; message field; removed developer Lock/Start Engine |
| Analytics | Removed developer JSON export and description; added configurable Database Manager link; repaired chart/export behaviour |
| Command menu | Removed developer custom tiles, trip options and theme selection |

## Automated checks performed

- 17 Python tests pass: actual login/password validation, role enforcement, signed messages, disconnected broker, publish failures, invalid commands, request-specific acknowledgements, previous-ack rejection, timeouts, raw telemetry metadata, retained-message handling, health states, historical-query validation, sorted/bounded history and CSV quoting, public URL filtering, gateway envelope validation and deduplication.
- Headless Chromium checks pass at 1440 px desktop and 390 px phone widths: login, developer-only removals, all five tabs, square controls, on/off toggles, gateway message, explicit rejection, CSV download, history error clearing, command menu, raw IMU data and five-second expiry, and return to Guest without a retained access token.
- No uncaught browser JavaScript errors were observed during those checks. The mobile header overlap was fixed and all developer tabs were rechecked for horizontal overflow.
- Visual review completed for login, Overview, Trips, System Health, Remote Control and Analytics, including mobile layouts.

Browser interaction tests supplied controlled API responses and telemetry; backend tests mocked the MQTT/Influx clients. These checks do not establish connectivity to your running containers or physical actuation. No Docker daemon, live database or vehicle was supplied in this environment.

## Five checks on your deployment

1. **Telemetry:** publish your normal STM32 packet. Confirm raw cards update; stop publishing and confirm they become `--` after five seconds. Try both object and JSON-string IMU values if used by your publisher.
2. **Connections:** check System Health while the gateway is publishing; pause it for over 15 seconds and confirm Disconnected. Verify database connectivity and stored-sample age. The telemetry rate is a website receive rate, not a measured network bandwidth.
3. **History:** open Analytics and select a field that exists in your `telemetry` measurement. Confirm dates/values against InfluxDB, export CSV and open Database Manager. Use a shorter range if the 10,000-point cap is reported.
4. **Commands:** first integrate `gateway/README.md`. Send Hoot, each indicator on and off, and hazards on and off; confirm both physical behaviour and the matching acknowledgement. Send a text message and verify its receipt at the gateway. Disconnect the gateway and confirm the UI does not claim success.
5. **Roles and layout:** open Developer on a phone and desktop. Switch to Guest/Operator and confirm their original panels remain available according to role. Keep your existing users database and certificates when rebuilding.

The illustrative trip's route, distance, speed and date/time are presentation examples. Replace them with the real simulation trace or logged trips in a future update; they must not be used as measured results.
